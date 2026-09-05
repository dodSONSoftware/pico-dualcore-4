# mqtt_client.py - Low-level MQTT wire protocol client (Core 0)
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import select
import socket
import struct
from binascii import hexlify

# Maximum remaining length (bytes) for an inbound MQTT packet. Derived from
# the protocol, not arbitrary: the largest spec-valid inbound frame is a
# write-config command carrying the worst-case valid configuration (every
# byte-bounded identity at 4-byte code points, which serialize 3x escaped,
# 128-CHARACTER command_id/target bounds, 253-byte broker address, all 16
# devices, all lists at their bounds) — 16,865 bytes in the conservative wire
# form (ASCII-escaped, default separators), pinned by the serialized-size
# invariant test in tests/test_config.py.
# 20 KiB keeps every valid command deliverable (16,384 dropped the worst-case
# command at the wire layer) while bounding what json.loads() can amplify:
# the parse peak (decoded string plus object graph) is a bounded multiple of
# this ceiling, and a MemoryError past it escapes to main.py's controlled-reset
# boundary rather than exhausting the heap silently. An oversized frame must
# fail the connection instead of letting sock.read(sz) request an allocation
# that could exhaust Pico RAM (256 KiB) — no hostile traffic needed.
MAX_INBOUND_PACKET_BYTES = 20 * 1024


class MQTTException(Exception):
    pass


class MQTTClient:
    def __init__(
        self,
        client_id,
        server,
        port=0,
        user=None,
        password=None,
        keepalive=0,
        ssl=None,
    ):
        if port == 0:
            port = 8883 if ssl else 1883
        self.client_id = client_id
        self.sock = None
        # Readiness poller for check_msg(), built once per socket and reused
        # across the ~100 ms polls instead of being constructed on every call.
        self.poller = None
        self._poller_sock = None
        self.server = server
        self.port = port
        self.ssl = ssl
        self.pid = 0
        self.cb = None
        self.user = user
        self.pswd = password
        self.keepalive = keepalive
        self.lw_topic = None
        self.lw_msg = None
        self.lw_qos = 0
        self.lw_retain = False

    def _send_str(self, s):
        self.sock.write(struct.pack("!H", len(s)))
        self.sock.write(s)

    def _abort_corrupt_inbound(self, reason):
        """Drop the connection over a corrupt inbound stream and raise: the
        stream is unreadable from here on, so close and let Core 0 reconnect."""
        try:
            self.sock.close()
        except MemoryError:
            raise
        except Exception:
            pass
        raise MQTTException(reason)

    def _read_required(self, size):
        # Read exactly `size` bytes, failing as a transport error on a short or
        # empty read. At EOF read(n) may return fewer bytes: that means the
        # link ended mid-frame and must surface as OSError (Core 0 reconnects
        # over it), not as the caller indexing short bytes (IndexError escapes
        # Core 0's recovery boundary and resets the MCU).
        data = self.sock.read(size)
        if data is None or len(data) != size:
            raise OSError(-1)
        return data

    def _recv_len(self):
        n = 0
        sh = 0
        while 1:
            b = self._read_required(1)[0]
            n |= (b & 0x7F) << sh
            if not b & 0x80:
                return n
            sh += 7
            if sh >= 28:
                # At most four remaining-length bytes; a fifth is a protocol
                # violation (an unbounded length), so fail instead of looping.
                self._abort_corrupt_inbound(
                    "Remaining length exceeds four bytes"
                )

    def next_packet_id(self):
        """Advance the packet ID and return it, wrapping 65535 to 1 (IDs are
        1..65535; 0 is reserved)."""
        self.pid += 1
        if self.pid > 65535:
            self.pid = 1
        return self.pid

    def set_callback(self, f):
        self.cb = f

    def set_last_will(self, topic, msg, retain=False, qos=0):
        # Validation, not an assert: MicroPython omits assert statements at
        # bytecode optimization >= 1, so protocol behavior must not depend on them.
        if not (0 <= qos <= 1):
            raise ValueError("Last-will qos must be 0 or 1 (QoS 2 is not supported)")
        if not topic:
            raise ValueError("Last-will topic is required")
        self.lw_topic = topic
        self.lw_msg = msg
        self.lw_qos = qos
        self.lw_retain = retain

    def connect(self, clean_session=True, timeout=None):
        self.sock = socket.socket()
        self.sock.settimeout(timeout)
        addr = socket.getaddrinfo(self.server, self.port)[0][-1]
        self.sock.connect(addr)
        if self.ssl:
            self.sock = self.ssl.wrap_socket(self.sock, server_hostname=self.server)
        premsg = bytearray(b"\x10\0\0\0\0\0")
        msg = bytearray(b"\x04MQTT\x04\x02\0\0")

        sz = 10 + 2 + len(self.client_id)
        msg[6] = clean_session << 1
        if self.user:
            sz += 2 + len(self.user) + 2 + len(self.pswd)
            msg[6] |= 0xC0
        if self.keepalive:
            if self.keepalive > 65535:
                raise MQTTException("Keepalive exceeds the 65535-second MQTT maximum")
            msg[7] |= self.keepalive >> 8
            msg[8] |= self.keepalive & 0x00FF
        if self.lw_topic:
            sz += 2 + len(self.lw_topic) + 2 + len(self.lw_msg)
            msg[6] |= 0x4 | (self.lw_qos & 0x1) << 3 | (self.lw_qos & 0x2) << 3
            msg[6] |= self.lw_retain << 5

        i = 1
        while sz > 0x7F:
            premsg[i] = (sz & 0x7F) | 0x80
            sz >>= 7
            i += 1
        premsg[i] = sz

        self.sock.write(premsg, i + 2)
        self.sock.write(msg)
        self._send_str(self.client_id)
        if self.lw_topic:
            self._send_str(self.lw_topic)
            self._send_str(self.lw_msg)
        if self.user:
            self._send_str(self.user)
            self._send_str(self.pswd)
        resp = self._read_required(4)
        if resp[0] != 0x20 or resp[1] != 0x02:
            # A malformed CONNACK means the stream is not what the handshake assumed.
            raise MQTTException("Invalid CONNACK")
        if resp[3] != 0:
            raise MQTTException(resp[3])
        return resp[2] & 1

    def disconnect(self):
        self.sock.write(b"\xe0\0")
        self.sock.close()

    def ping(self, timeout_sec=None):
        """Send PINGREQ and wait for the matching PINGRESP (optionally
        timeout-bounded); a failed timeout installation propagates into
        recovery rather than falling through to an unbounded wait."""
        if timeout_sec is not None:
            # The bounded wait depends on this timeout being active.
            self.sock.settimeout(timeout_sec)
        try:
            self.sock.write(b"\xc0\0")
            while 1:
                # wait_msg() consumes PINGRESP internally and returns None.
                if self.wait_msg() is None:
                    return
        finally:
            if timeout_sec is not None:
                # Restoring blocking mode is not best-effort: the next
                # operation assumes it, so a failed restoration propagates
                # into Core 0's recovery instead of marking the exchange done.
                self.sock.settimeout(None)

    def publish(self, topic, msg, retain=False, qos=0, packet_id=None, timeout_ms=None, splice_fragment=None):
        """Publish an application message; optional packet_id (else auto-increment) and timeout_ms bound the QoS 1 exchange.

        With ``splice_fragment``, the frame's final bytes are written
        segment by segment: ``msg`` without its closing brace, then a comma,
        the fragment, then the brace. The wire bytes are identical to a
        single pre-joined buffer, but no allocation is ever sized to the
        whole spliced frame (see the write below)."""
        if qos == 2:
            # Reject before a single frame byte goes out (an assert here would
            # vanish under MicroPython bytecode optimization and the frame
            # would be transmitted unacked).
            raise MQTTException("QoS 2 is not supported")
        pkt = bytearray(b"\x30\0\0\0")
        pkt[0] |= qos << 1 | retain
        sz = 2 + len(topic) + len(msg)
        if splice_fragment is not None:
            # The splice (comma + fragment + brace) replaces msg's closing
            # brace: the spliced frame body is len(msg) + len(fragment) + 1.
            sz += len(splice_fragment) + 1
        if qos > 0:
            sz += 2
        if sz > 2097151:
            # The MQTT remaining-length field tops out at 2097151.
            raise MQTTException("Publish size exceeds the MQTT remaining-length maximum")
        i = 1
        while sz > 0x7F:
            pkt[i] = (sz & 0x7F) | 0x80
            sz >>= 7
            i += 1
        pkt[i] = sz
        if qos > 0:
            if packet_id is None:
                pid = self.next_packet_id()
            else:
                pid = packet_id
        # Bound the WHOLE QoS 1 exchange — the PUBLISH frame writes included:
        # a blackholed link whose writes stop making progress then surfaces
        # as a bounded error (connection dead, Core 0 recovery fires) instead
        # of wedging Core 0 inside sock.write(). The timeout is installed
        # before byte 1 goes out and restored only after the exchange.
        timed = qos == 1 and timeout_ms is not None
        if timed:
            self.sock.settimeout(timeout_ms / 1000.0)
        try:
            self.sock.write(pkt, i + 1)
            self._send_str(topic)
            if qos > 0:
                # Pack the packet id only now: it reuses the opcode/length
                # bytes already written (wire order: header, topic, packet id).
                struct.pack_into("!H", pkt, 0, pid)
                self.sock.write(pkt, 2)
            if splice_fragment is None:
                self.sock.write(msg)
            else:
                # Segment the spliced tail: a zero-copy view of msg minus its
                # closing brace, then the splice itself. TCP is a byte
                # stream, so the broker receives exactly the pre-joined
                # frame -- but no single allocation is sized to the whole
                # frame. The old pre-joined write needed the full frame
                # contiguously, and after the startup imports the heap is
                # fragmented: the largest free block can be smaller than the
                # frame even with tens of KiB total free, so the first
                # post-startup publish (the startup log) hit a deterministic
                # MemoryError and reset loop.
                view = memoryview(msg)
                self.sock.write(view[: len(msg) - 1])
                self.sock.write(b",")
                self.sock.write(splice_fragment)
                self.sock.write(b"}")
            if qos == 1:
                while 1:
                    op = self.wait_msg()
                    if op == 0x40:
                        sz = self._read_required(1)
                        if sz != b"\x02":
                            # A PUBACK is exactly a 2-byte packet id.
                            self._abort_corrupt_inbound(
                                "PUBACK with unexpected remaining length"
                            )
                        rcv_pid = self._read_required(2)
                        rcv_pid = rcv_pid[0] << 8 | rcv_pid[1]
                        if pid == rcv_pid:
                            return
        finally:
            if timed:
                # Restoring blocking mode is not best-effort: the next
                # operation assumes it, so a failed restoration propagates
                # into Core 0's recovery instead of reporting the publish done.
                self.sock.settimeout(None)

    def subscribe(self, topic, qos=0):
        if self.cb is None:
            raise MQTTException("Subscribe callback is not set")
        # The Remaining Length is encoded in one byte (valid through 127), and
        # the body is topic + 5 (2 packet-id + 2 topic-length + 1 QoS), so
        # above 122 topic bytes the length byte would gain the continuation
        # bit and the packet would be malformed. config.py (MAX_MQTT_TOPIC_BYTES)
        # is the authoritative boundary; this is defensive transport validation.
        if len(topic) > 122:
            raise MQTTException(
                "Subscribe topic exceeds the single-byte remaining-length bound"
            )
        pkt = bytearray(b"\x82\0\0\0")
        pid = self.next_packet_id()
        struct.pack_into("!BH", pkt, 1, 2 + 2 + len(topic) + 1, pid)
        self.sock.write(pkt)
        self._send_str(topic)
        self.sock.write(qos.to_bytes(1, "little"))
        while 1:
            op = self.wait_msg()
            if op == 0x90:
                resp = self._read_required(4)
                if resp[1] != pkt[2] or resp[2] != pkt[3]:
                    raise MQTTException("Invalid SUBACK packet identifier")
                if resp[3] == 0x80:
                    raise MQTTException(resp[3])
                return

    # Wait for a single incoming MQTT message and process it: subscribed
    # messages go to the callback set via set_callback(), internal messages
    # are processed here.
    def wait_msg(self):
        # Read in the caller's socket mode: publish/ping/subscribe and
        # check_msg run in blocking-with-timeout mode, so every read here is
        # bounded and returns a full length. wait_msg must not change the
        # mode: setblocking(True) == settimeout(None) in MicroPython, which
        # would silently clear the caller's timeout.
        res = self.sock.read(1)
        if res is None:
            return None
        if res == b"":
            raise OSError(-1)
        if res == b"\xd0":  # PINGRESP
            sz = self._read_required(1)[0]
            if sz != 0:
                # PINGRESP carries no payload; anything else is a corrupt stream.
                self._abort_corrupt_inbound(
                    "PINGRESP with non-zero remaining length"
                )
            return None
        op = res[0]
        if op & 0xF0 != 0x30:
            return op
        # Validate the QoS bits before the payload is read and before the
        # callback runs (which feeds the command protocol): QoS 2 is outside
        # this client's profile and QoS 3 is spec-invalid for PUBLISH, so a
        # nonconforming frame is dropped at the wire layer, never delivered.
        if op & 6 == 4:
            self._abort_corrupt_inbound("Inbound QoS 2 is not supported")
        if op & 6 == 6:
            self._abort_corrupt_inbound("Invalid PUBLISH QoS")
        sz = self._recv_len()
        if sz > MAX_INBOUND_PACKET_BYTES:
            # Oversized inbound packet: sock.read(sz) would request an
            # allocation large enough to exhaust Pico RAM. Drop before
            # any payload is allocated.
            self._abort_corrupt_inbound(
                "Inbound packet remaining length {} exceeds {}".format(
                    sz, MAX_INBOUND_PACKET_BYTES
                )
            )
        # Validate the declared topic length against the remaining length
        # *before* reading: a corrupt stream can declare a topic length far
        # larger than it carries (remaining 2 with a 65535-byte topic), and
        # trusting that would request a sock.read() allocation approaching
        # 64 KiB on a 256 KB device. Inconsistent frames are dropped the same
        # as oversized ones, before any payload is allocated.
        if sz < 2:
            self._abort_corrupt_inbound(
                "Inbound packet too short for a topic length field"
            )
        remaining = sz - 2
        topic_len = self._read_required(2)
        topic_len = (topic_len[0] << 8) | topic_len[1]
        if topic_len > remaining:
            self._abort_corrupt_inbound(
                "Inbound topic length {} exceeds remaining length {}".format(
                    topic_len, remaining
                )
            )
        topic = self._read_required(topic_len)
        remaining -= topic_len
        if op & 6:
            if remaining < 2:
                self._abort_corrupt_inbound(
                    "Inbound packet too short for a packet identifier"
                )
            remaining -= 2
            pid = self._read_required(2)
            pid = pid[0] << 8 | pid[1]
        msg = self._read_required(remaining)
        self.cb(topic, msg)
        if op & 6 == 2:
            pkt = bytearray(b"\x40\x02\0\0")
            struct.pack_into("!H", pkt, 2, pid)
            self.sock.write(pkt)
        return op

    def _ready_poller(self):
        """Return a poller registered on the current socket, built once and
        reused for the life of the connection (no per-poll select churn on
        the hot path); a reconnect (new socket) discards the old poller."""
        if self.poller is None or self._poller_sock is not self.sock:
            self.poller = select.poll()
            self.poller.register(self.sock, select.POLLIN)
            self._poller_sock = self.sock
        return self.poller

    def _socket_ready(self, poller):
        """Non-blocking readiness: True iff a packet has started. Prefers the
        allocation-free ipoll() with a zero timeout, falling back to poll()
        where ipoll is unavailable (the CPython host suite)."""
        ipoll = getattr(poller, "ipoll", None)
        if ipoll is not None:
            return any(ipoll(0))
        return bool(poller.poll(0))

    # Checks whether a pending message from server is available. If not,
    # returns immediately with None; otherwise does the same processing as
    # wait_msg.
    def check_msg(self, timeout_sec):
        # Readiness is decided with a poll, not a non-blocking read: one
        # readable byte only means a packet has *started* arriving, and the
        # rest of the parse must run where read(n) is guaranteed to return n
        # bytes — MicroPython documents that read(n) may return fewer in
        # non-blocking mode, so a PUBLISH split across TCP reads would
        # short-read and deliver a corrupt frame.
        if not self._socket_ready(self._ready_poller()):
            # No packet started: return, leaving the socket mode untouched.
            return None
        # A packet is in flight: finish it with a finite timeout so a
        # mid-packet stall surfaces as a timeout (recovery), not a hang.
        self.sock.settimeout(timeout_sec)
        try:
            return self.wait_msg()
        finally:
            # Restore blocking mode: a finite timeout left behind would
            # corrupt the next operation's bounded wait.
            self.sock.settimeout(None)
