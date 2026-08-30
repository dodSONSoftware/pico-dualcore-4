# network_diagnostics.py - Core 0 bounded network reachability probes
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Bounded, single-shot reachability probes for the Core 0 diagnostics.

This module provides exactly three small helpers and nothing more:

- icmp_echo_supported(): feature-detect whether the port can create a raw
  ICMP socket at all (expected False on RP2/cyw43; callers must treat a
  False result as "not testable", not "unreachable", and must NOT fake the
  probe with a UDP send);
- probe_gateway(): ONE bounded IPv4 ICMP echo request/reply;
- probe_dns_server(): ONE bounded UDP DNS query to the configured server.

No ping loop, no traceroute, no DNS parser, no history, no background work.
Every socket gets a finite timeout and is closed in a finally block. A
MemoryError always propagates (heap exhaustion is fatal, not a failed
diagnostic); any ordinary failure returns a bounded "(False, None)".
"""

import socket
import time


# One bounded probe attempt. Internal constant: not user-configurable.
NETWORK_DIAGNOSTIC_PROBE_TIMEOUT_MS = 750

# Fixed identifiers. One probe at a time (Core 0 runs at most one stage per
# pass), so no randomization or per-run identity is needed.
_ICMP_ECHO_IDENTIFIER = 0x4E44  # "ND"
_DNS_TXID = 0x4E44  # "ND"

_ICMP_ECHO_PAYLOAD = b"nd-echo-payload1"  # exactly 16 bytes

# Fixed 36-byte DNS query: question "diagnostic.invalid." A/IN. Byte literal
# on purpose — no JSON, no dicts, no allocation beyond this constant in the
# packet path.
_DNS_QUERY = (
    b"\x4e\x44"  # transaction id
    b"\x01\x00"  # RD
    b"\x00\x01"  # QDCOUNT
    b"\x00\x00"  # ANCOUNT
    b"\x00\x00"  # NSCOUNT
    b"\x00\x00"  # ARCOUNT
    b"\x0adiagnostic\x07invalid\x00"
    b"\x00\x01"  # QTYPE A
    b"\x00\x01"  # QCLASS IN
)

_ICMP_ECHO_SUPPORTED = None


def _close_quietly(sock):
    if sock is not None:
        try:
            sock.close()
        except MemoryError:
            raise
        except Exception:
            pass


def icmp_echo_supported():
    """Whether the port can create a raw ICMP socket (feature-detected once).

    Expected to be False on the RP2/cyw43 ports. When False the gateway
    fields report supported=False with null reachable/latency — the probe is
    never faked with a UDP send.
    """
    global _ICMP_ECHO_SUPPORTED
    if _ICMP_ECHO_SUPPORTED is not None:
        return _ICMP_ECHO_SUPPORTED
    supported = False
    if getattr(socket, "SOCK_RAW", None) is not None:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, 1)
            supported = True
        except MemoryError:
            raise
        except Exception:
            supported = False
        finally:
            _close_quietly(sock)
    _ICMP_ECHO_SUPPORTED = supported
    return supported


def _icmp_checksum(data):
    """One's-complement 16-bit internet checksum (integer only)."""
    total = 0
    for index in range(0, len(data) - 1, 2):
        total += (data[index] << 8) + data[index + 1]
    if len(data) % 2:
        total += data[-1] << 8
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _build_echo_request():
    """24-byte IPv4 ICMP Echo Request: type 8/code 0, seq 1, 16-byte payload."""
    packet = bytearray(24)
    packet[0] = 8  # ICMP type: echo request
    packet[1] = 0  # ICMP code
    packet[4] = (_ICMP_ECHO_IDENTIFIER >> 8) & 0xFF
    packet[5] = _ICMP_ECHO_IDENTIFIER & 0xFF
    packet[6] = 0  # sequence
    packet[7] = 1
    packet[8:24] = _ICMP_ECHO_PAYLOAD
    checksum = _icmp_checksum(bytes(packet))
    packet[2] = (checksum >> 8) & 0xFF
    packet[3] = checksum & 0xFF
    return bytes(packet)


def probe_gateway(gateway, timeout_ms=NETWORK_DIAGNOSTIC_PROBE_TIMEOUT_MS):
    """ONE bounded ICMP echo request/reply. Returns (reachable, latency_ms).

    Success requires a reply with type 0/code 0 and the matching identifier;
    latency is the tick-diff between send and reply. Timeout or any ordinary
    error returns (False, None). A MemoryError propagates.
    """
    if not icmp_echo_supported():
        return (False, None)
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, 1)
        sock.settimeout(timeout_ms / 1000)
        started_ms = time.ticks_ms()
        sock.sendto(_build_echo_request(), (gateway, 0))
        data = sock.recvfrom(64)[0]
        if len(data) >= 6 and data[0] == 0 and data[1] == 0 \
                and ((data[4] << 8) + data[5]) == _ICMP_ECHO_IDENTIFIER:
            return (True, time.ticks_diff(time.ticks_ms(), started_ms))
        return (False, None)
    except MemoryError:
        raise
    except Exception:
        return (False, None)
    finally:
        _close_quietly(sock)


def probe_dns_server(dns_server, timeout_ms=NETWORK_DIAGNOSTIC_PROBE_TIMEOUT_MS):
    """ONE bounded UDP DNS query to the configured server:53.

    Reachable when a response of at least 3 bytes arrives with the matching
    transaction id and the QR bit set; any RCODE (NOERROR, NXDOMAIN, SERVFAIL)
    proves the server answered. A mismatched transaction id returns
    (False, None) — never a false success. Timeout/error returns
    (False, None). Never uses socket.getaddrinfo(). A MemoryError propagates.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout_ms / 1000)
        started_ms = time.ticks_ms()
        sock.sendto(_DNS_QUERY, (dns_server, 53))
        data = sock.recvfrom(512)[0]
        if len(data) >= 3 and ((data[0] << 8) + data[1]) == _DNS_TXID \
                and (data[2] & 0x80):
            return (True, time.ticks_diff(time.ticks_ms(), started_ms))
        return (False, None)
    except MemoryError:
        raise
    except Exception:
        return (False, None)
    finally:
        _close_quietly(sock)
