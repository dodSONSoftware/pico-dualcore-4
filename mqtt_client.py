# mqtt_client.py - Low-level MQTT wire protocol client (Core 0)
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import select
import socket
import struct
from binascii import hexlify

# Maximum remaining length (bytes) for an inbound MQTT packet.
#
# MCU-scale ceiling for the receive path (the outbound ceiling is
# message_serializer.MAX_OUTBOUND_MESSAGE_BYTES). The inbound topics are the
# command and info-response topics only, so every legitimate inbound message
# is well under this limit. A broker-side bug that publishes an oversized
# frame must fail the connection instead of letting sock.read(sz) request an
# allocation large enough to exhaust Pico RAM (256 KiB) — no hostile traffic
# is required for that.
MAX_INBOUND_PACKET_BYTES = 16 * 1024


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
        """Drop the connection over a corrupt inbound stream and raise.

        The byte stream is unreadable from here on: the socket is closed and the failure propagates (Core 0's recovery path reconnects)."""
        try:
            self.sock.close()
        except MemoryError:
            raise
        except Exception:
            pass
        raise MQTTException(reason)

    def _recv_len(self):
        n = 0
        sh = 0
        while 1:
            b = self.sock.read(1)[0]
            n |= (b & 0x7F) << sh
            if not b & 0x80:
                return n
            sh += 7
            if sh >= 28:
                # MQTT allows at most four remaining-length bytes; a fifth
                # continuation byte is a protocol violation (and, on a byte
                # stream, an unbounded length), so fail the packet instead of
                # looping forever on reads.
                self._abort_corrupt_inbound(
                    "Remaining length exceeds four bytes"
                )

    def next_packet_id(self):
        """Advance the packet ID and return it, wrapping 65535 back to 1.

        IDs are 1..65535 (0 is reserved); the QoS 1, subscribe, and probe paths all draw from this one helper."""
        self.pid += 1
        if self.pid > 65535:
            self.pid = 1
        return self.pid

    def set_callback(self, f):
        self.cb = f

    def set_last_will(self, topic, msg, retain=False, qos=0):
        # Parameter validation, not an invariant: MicroPython omits assert
        # statements at bytecode optimization >= 1, so protocol behavior
        # must never depend on them.
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
        # print(hex(len(msg)), hexlify(msg, ":"))
        self._send_str(self.client_id)
        if self.lw_topic:
            self._send_str(self.lw_topic)
            self._send_str(self.lw_msg)
        if self.user:
            self._send_str(self.user)
            self._send_str(self.pswd)
        resp = self.sock.read(4)
        if resp[0] != 0x20 or resp[1] != 0x02:
            # A malformed CONNACK means the byte stream is not what the
            # handshake assumed; failing here (asserts are omitted under
            # MicroPython bytecode optimization) is the only behavior.
            raise MQTTException("Invalid CONNACK")
        if resp[3] != 0:
            raise MQTTException(resp[3])
        return resp[2] & 1

    def disconnect(self):
        self.sock.write(b"\xe0\0")
        self.sock.close()

    def ping(self, timeout_sec=None):
        """Send PINGREQ and wait for the matching PINGRESP (optionally timeout-bounded).

        A failed timeout installation propagates into recovery rather than falling through to an unbounded wait."""
        if timeout_sec is not None:
            # The bounded wait depends on this timeout being active, so a
            # failed installation must not be swallowed: let it propagate
            # into Core 0's network recovery instead of entering the wait.
            self.sock.settimeout(timeout_sec)
        try:
            self.sock.write(b"\xc0\0")
            while 1:
                # wait_msg() consumes PINGRESP internally and returns None.
                if self.wait_msg() is None:
                    return
        finally:
            if timeout_sec is not None:
                # Restoring normal blocking mode is not best-effort: the next
                # operation assumes the socket is back in blocking mode, so a
                # failed restoration is a connection failure — let it
                # propagate into Core 0's recovery (as check_msg() does for
                # its own restoration) instead of marking the exchange done.
                self.sock.settimeout(None)

    def publish(self, topic, msg, retain=False, qos=0, packet_id=None, timeout_ms=None):
        """Publish an application message; optional packet_id (else auto-increment) and timeout_ms bound the QoS 1 exchange."""
        if qos == 2:
            # Unsupported protocol level: reject before a single frame byte
            # is transmitted (an assert here would vanish under MicroPython
            # bytecode optimization and the frame would go out unacked).
            raise MQTTException("QoS 2 is not supported")
        pkt = bytearray(b"\x30\0\0\0")
        pkt[0] |= qos << 1 | retain
        sz = 2 + len(topic) + len(msg)
        if qos > 0:
            sz += 2
        if sz > 2097151:
            # MQTT's remaining-length field tops out at 2097151; beyond it
            # the frame could not be encoded at all.
            raise MQTTException("Publish size exceeds the MQTT remaining-length maximum")
        i = 1
        while sz > 0x7F:
            pkt[i] = (sz & 0x7F) | 0x80
            sz >>= 7
            i += 1
        pkt[i] = sz
        # print(hex(len(pkt)), hexlify(pkt, ":"))
        if qos > 0:
            # Use provided packet_id or auto-increment (the packet id is packed
            # into the frame at the point the existing wire order writes it).
            if packet_id is None:
                pid = self.next_packet_id()
            else:
                pid = packet_id
        # Bound the WHOLE QoS 1 exchange — the PUBLISH frame writes included —
        # when a timeout is specified: a blackholed link whose writes stop
        # making progress then surfaces as a bounded error (which marks the
        # connection dead and lets Core 0's network recovery fire) instead of
        # wedging Core 0 inside sock.write(), where even the Core 1 heartbeat
        # check could never run. The timeout is installed before byte 1 of
        # the frame goes out and restored only after the exchange finishes.
        # The bounded exchange depends on this timeout being active, so a
        # failed installation must not be swallowed: let it propagate into
        # Core 0's network recovery before any byte is transmitted.
        timed = qos == 1 and timeout_ms is not None
        if timed:
            self.sock.settimeout(timeout_ms / 1000.0)
        try:
            self.sock.write(pkt, i + 1)
            self._send_str(topic)
            if qos > 0:
                # Pack the packet id only now — it reuses the opcode/length
                # bytes already written above, so the wire order is
                # header, topic, packet id, payload (as before this change).
                struct.pack_into("!H", pkt, 0, pid)
                self.sock.write(pkt, 2)
            self.sock.write(msg)
            if qos == 1:
                while 1:
                    op = self.wait_msg()
                    if op == 0x40:
                        sz = self.sock.read(1)
                        if sz != b"\x02":
                            # A PUBACK is exactly a 2-byte packet id; any
                            # other length corrupts the stream from here on.
                            self._abort_corrupt_inbound(
                                "PUBACK with unexpected remaining length"
                            )
                        rcv_pid = self.sock.read(2)
                        rcv_pid = rcv_pid[0] << 8 | rcv_pid[1]
                        if pid == rcv_pid:
                            return
        finally:
            if timed:
                # Restoring normal blocking mode is not best-effort: the next
                # operation assumes the socket is back in blocking mode, so a
                # failed restoration is a connection failure — let it
                # propagate into Core 0's recovery (as check_msg() does for
                # its own restoration) instead of reporting the publish done.
                self.sock.settimeout(None)

    def subscribe(self, topic, qos=0):
        if self.cb is None:
            raise MQTTException("Subscribe callback is not set")
        # The Remaining Length below is encoded in exactly one byte (valid
        # through 127), so the body must fit: 2 packet-id + 2 topic-length
        # + topic + 1 requested-QoS = topic + 5. Above 122 topic bytes the
        # first length byte would gain the continuation bit (0x80) and the
        # packet would be malformed. config.py is the authoritative
        # boundary (MAX_MQTT_TOPIC_BYTES); this is defensive transport
        # validation, as for the keepalive in connect().
        if len(topic) > 122:
            raise MQTTException(
                "Subscribe topic exceeds the single-byte remaining-length bound"
            )
        pkt = bytearray(b"\x82\0\0\0")
        pid = self.next_packet_id()
        struct.pack_into("!BH", pkt, 1, 2 + 2 + len(topic) + 1, pid)
        # print(hex(len(pkt)), hexlify(pkt, ":"))
        self.sock.write(pkt)
        self._send_str(topic)
        self.sock.write(qos.to_bytes(1, "little"))
        while 1:
            op = self.wait_msg()
            if op == 0x90:
                resp = self.sock.read(4)
                # print(resp)
                if resp[1] != pkt[2] or resp[2] != pkt[3]:
                    raise MQTTException("Invalid SUBACK packet identifier")
                if resp[3] == 0x80:
                    raise MQTTException(resp[3])
                return

    # Wait for a single incoming MQTT message and process it.
    # Subscribed messages are delivered to a callback previously
    # set by .set_callback() method. Other (internal) MQTT
    # messages processed internally.
    def wait_msg(self):
        # Read in the caller's socket mode. publish/ping/subscribe and
        # check_msg all run in blocking-with-timeout mode, so every read here
        # is bounded by their timeout and a link that stalls after the first
        # frame byte surfaces as a timeout instead of blocking forever (and,
        # crucially, a read always returns a full length instead of a short
        # one). wait_msg must not change the mode: setblocking(True) ==
        # settimeout(None) in MicroPython, which would silently clear the
        # caller's timeout.
        res = self.sock.read(1)
        if res is None:
            return None
        if res == b"":
            raise OSError(-1)
        if res == b"\xd0":  # PINGRESP
            sz = self.sock.read(1)[0]
            if sz != 0:
                # PINGRESP carries no payload; anything else is a corrupt
                # stream and the leftover bytes make it unreadable.
                self._abort_corrupt_inbound(
                    "PINGRESP with non-zero remaining length"
                )
            return None
        op = res[0]
        if op & 0xF0 != 0x30:
            return op
        # Validate the QoS bits before the payload is read and before the
        # callback runs: QoS 2 is outside this client's protocol profile and
        # QoS 3 is invalid for PUBLISH by the MQTT spec. The callback feeds
        # the command protocol, so a nonconforming frame must be dropped at
        # the wire layer, never delivered to the application and rejected
        # afterward (and the old assert was build-dependent anyway).
        if op & 6 == 4:
            self._abort_corrupt_inbound("Inbound QoS 2 is not supported")
        if op & 6 == 6:
            self._abort_corrupt_inbound("Invalid PUBLISH QoS")
        sz = self._recv_len()
        if sz > MAX_INBOUND_PACKET_BYTES:
            # An oversized inbound packet is a broker-side fault, not a
            # legitimate message: sock.read(sz) below would request an
            # allocation large enough to exhaust Pico RAM. Reject it before
            # any payload is allocated and drop the connection.
            self._abort_corrupt_inbound(
                "Inbound packet remaining length {} exceeds {}".format(
                    sz, MAX_INBOUND_PACKET_BYTES
                )
            )
        # Validate every variable-sized read against the remaining length
        # *before* it happens: the topic length (and packet id) are declared
        # by the packet itself, so a corrupt stream can declare a topic
        # length far larger than the remaining length actually carried (for
        # example remaining length 2 with a 65535-byte topic). Trusting that
        # declaration would request a sock.read() allocation approaching
        # 64 KiB on a 256 KB device and drive sz negative. Treat an
        # internally inconsistent frame the same as an oversized one: drop
        # the connection before any payload is allocated.
        if sz < 2:
            self._abort_corrupt_inbound(
                "Inbound packet too short for a topic length field"
            )
        remaining = sz - 2
        topic_len = self.sock.read(2)
        topic_len = (topic_len[0] << 8) | topic_len[1]
        if topic_len > remaining:
            self._abort_corrupt_inbound(
                "Inbound topic length {} exceeds remaining length {}".format(
                    topic_len, remaining
                )
            )
        topic = self.sock.read(topic_len)
        remaining -= topic_len
        if op & 6:
            if remaining < 2:
                self._abort_corrupt_inbound(
                    "Inbound packet too short for a packet identifier"
                )
            remaining -= 2
            pid = self.sock.read(2)
            pid = pid[0] << 8 | pid[1]
        msg = self.sock.read(remaining)
        self.cb(topic, msg)
        if op & 6 == 2:
            pkt = bytearray(b"\x40\x02\0\0")
            struct.pack_into("!H", pkt, 2, pid)
            self.sock.write(pkt)
        return op

    def _ready_poller(self):
        """Return a poller registered on the current socket, building it once.

        Created at the first readiness check and reused for the life of the connection -- avoiding per-poll select construction churn on the hot path. A reconnect (new socket object) discards the old poller."""
        if self.poller is None or self._poller_sock is not self.sock:
            self.poller = select.poll()
            self.poller.register(self.sock, select.POLLIN)
            self._poller_sock = self.sock
        return self.poller

    def _socket_ready(self, poller):
        """Non-blocking readiness: True iff the socket has a packet started.

        Prefers the allocation-free ipoll() with a zero timeout, falling back to poll() where ipoll is unavailable (the CPython host suite)."""
        ipoll = getattr(poller, "ipoll", None)
        if ipoll is not None:
            return any(ipoll(0))
        return bool(poller.poll(0))

    # Checks whether a pending message from server is available.
    # If not, returns immediately with None. Otherwise, does
    # the same processing as wait_msg.
    def check_msg(self, timeout_sec):
        # Readiness is decided with a poll, not a non-blocking read: one
        # readable byte only means a packet has *started* arriving, and the
        # rest of the parse must run where read(n) is guaranteed to return n
        # bytes. Parsing in non-blocking mode is unsafe on a byte stream —
        # MicroPython documents that read(n) may return fewer bytes than
        # requested there — so a PUBLISH split across TCP reads would
        # short-read its topic or payload and deliver a corrupt frame (or
        # raise a spurious disconnect) instead of waiting for the rest of the
        # packet. The poller itself is built once per socket (see
        # _ready_poller) and reused across the ~100 ms polls.
        if not self._socket_ready(self._ready_poller()):
            # No packet has started: return immediately, leaving the socket in
            # whatever mode the caller left it in (the poll consumed nothing).
            return None
        # A packet is in flight: finish it on a blocking-with-finite-timeout
        # socket so every read returns a full length, and a link that stalls
        # mid-packet surfaces as a timeout (network recovery) instead of a hang
        # or a short read.
        self.sock.settimeout(timeout_sec)
        try:
            return self.wait_msg()
        finally:
            # Restore normal blocking mode: the run loop polls on a tight
            # cadence, so leaving a finite timeout (or a non-blocking socket)
            # behind would corrupt the next operation's bounded wait.
            self.sock.settimeout(None)
