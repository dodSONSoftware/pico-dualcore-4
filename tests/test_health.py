# test_health.py - Host-side behavioral tests for health message generation
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

"""Host-side regression tests for Core 1 health payload generation.

These tests drive the PRODUCTION builder ``core1._build_health_payload`` (not a
copy) under a controllable clock and minimal MicroPython stand-ins, and assert
on the externally observable payload values: Core-1 liveness, runtime/uptime,
queue/state info, device state, and the conditional fields. A deliberate change
to the production builder must fail the corresponding test.
"""

import gc
import importlib
import json
import os as _real_os
import pathlib
import sys
import time as _real_time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import load_config, split_config  # noqa: E402
from intercore import (  # noqa: E402
    InterCore,
    KIND_HEALTH,
    RETENTION_PRIORITY_HEALTH,
)
from message_protocol import format_utc_epoch_ms  # noqa: E402
from message_serializer import serialize_and_validate_message  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[1]

# Time geometry shared by the tests: boot at 100000ms, "now" at 110000ms
# (10s uptime), Core-1 activity a little before "now".
BOOT_TICKS_MS = 100000
NOW_MS = 110000


class FakeTime:
    """Controllable monotonic clock for the host (mirrors MicroPython ticks)."""

    def __init__(self, now_ms=NOW_MS):
        self.now_ms = now_ms

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def ticks_add(self, base, delta):
        return base + delta

    def sleep_ms(self, ms):
        self.now_ms += ms

    def __getattr__(self, name):
        return getattr(_real_time, name)


class FakeMachine:
    """Minimal MicroPython ``machine`` stand-in (CPU frequency source)."""

    @staticmethod
    def freq():
        return 125000000


class _Uname:
    sysname = "MicroPython"
    nodename = "pico"
    release = "v1.23.0"
    version = "v1.23.0"
    machine = "Raspberry Pi Pico W with RP2040"


class FakeOs:
    """``os`` stand-in reporting a Pico W machine string from uname()."""

    def uname(self):
        return _Uname()

    def __getattr__(self, name):
        return getattr(_real_os, name)


class FakeSystemInformation:
    """Minimal stand-in exposing only the device-status hook the builder uses."""

    def __init__(self, configured, active):
        self._configured = configured
        self._active = active

    def get_devices(self):
        return {"configured": self._configured, "active": self._active}


def _install_fakes(fake_time):
    sys.modules["time"] = fake_time
    sys.modules["machine"] = FakeMachine()
    sys.modules["os"] = FakeOs()


def _reload_core1_under_fakes():
    """Import/reload the core1 chain with the fakes authoritative.

    core1 binds time/machine/os (and uptime) from sys.modules at import time,
    so any cached module is reloaded in dependency order before core1 itself.
    """
    names = (
        "hardware",
        "system_information",
        "device_manager",
        "device_factory",
        "devices",
        "devices.system_information",
        "devices.system_information.system_information_device",
        "uptime",
        "core1",
    )
    for name in names[:-1]:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
    core1 = sys.modules.get("core1")
    if core1 is None:
        core1 = importlib.import_module("core1")
    else:
        core1 = importlib.reload(core1)
    return core1


def _valid_utc_snapshot(age_ms=10000):
    """A valid UTC snapshot taken ``age_ms`` before "now"."""
    return {
        "utc_epoch_ms": 200000,
        "ticks_ms": NOW_MS - age_ms,
    }


class HealthEnv:
    """Runs the production health builder under host fakes and shims.

    Installs controllable time/machine/os stand-ins, reloads the core1 chain so
    they are authoritative, and exposes a small API to drive the builder's
    inputs (state mailboxes, device status, free heap, clock). Fakes and the
    ``gc.mem_free`` shim are restored on close().
    """

    def __init__(self):
        self._fake_time = FakeTime(NOW_MS)
        self._free_heap = 100000
        self._configured = 1
        self._active = 1

        self._saved_modules = {
            name: sys.modules.get(name) for name in ("time", "machine", "os")
        }
        self._had_mem_free = hasattr(gc, "mem_free")
        self._saved_mem_free = getattr(gc, "mem_free", None)

        _install_fakes(self._fake_time)
        self.core1 = _reload_core1_under_fakes()
        self.bus = InterCore(outbound_max=16, event_max=4)
        self.config = self._core1_config()
        # MicroPython exposes gc.mem_free(); the host does not. Shim it to a
        # controllable value so free-heap fields are deterministic.
        gc.mem_free = lambda: self._free_heap

    def _core1_config(self):
        config = load_config(str(ROOT / "config.json"))
        _core0, core1_config, _bus = split_config(config)
        return core1_config

    # -- builder inputs ------------------------------------------------
    def set_network(self, snapshot):
        self.bus.state_mailboxes.set_network_snapshot(snapshot)

    def set_utc(self, snapshot):
        self.bus.state_mailboxes.set_utc_snapshot(snapshot)

    def set_core_1_activity(self, ticks_ms):
        self.bus.state_mailboxes.set_core_1_activity_ms(ticks_ms)

    def set_hardware(self, hardware):
        self.bus.state_mailboxes.set_hardware(hardware)

    def set_devices(self, configured, active):
        self._configured = configured
        self._active = active

    def set_free_heap(self, value):
        self._free_heap = value

    def set_clock(self, now_ms):
        self._fake_time.now_ms = now_ms

    def set_healthy_baseline(self):
        """Establish a fully-healthy baseline (except UTC) a test can perturb."""
        self.set_network({
            "wifi_connected": True,
            "mqtt_connected": True,
            "network_stack_ready": True,
            "rssi": -50,
        })
        self.set_core_1_activity(NOW_MS - 100)  # recent -> active
        self.set_hardware({
            "hardware_type": "pico_w",
            "machine": "Raspberry Pi Pico W with RP2040",
            "minimum_free_heap_bytes": 65536,
            "last_reset_cause": "power_on_reset",
        })
        self.set_devices(1, 1)
        self.set_free_heap(100000)
        self.set_clock(NOW_MS)

    # -- run the production builder -----------------------------------
    def build(self, boot_ticks_ms=BOOT_TICKS_MS):
        system_information = FakeSystemInformation(self._configured, self._active)
        uptime_state = self.core1.create_uptime_state(boot_ticks_ms)
        return self.core1._build_health_payload(
            self.bus,
            uptime_state,
            self.config,
            system_information,
        )

    # -- teardown ------------------------------------------------------
    def close(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if self._had_mem_free:
            gc.mem_free = self._saved_mem_free
        elif hasattr(gc, "mem_free"):
            delattr(gc, "mem_free")


@pytest.fixture
def health():
    env = HealthEnv()
    yield env
    env.close()


# ---------------------------------------------------------------------------
# Core classification (healthy vs degraded) and liveness
# ---------------------------------------------------------------------------

def test_healthy_state_payload(health):
    """A fully-healthy state reports healthy with no degraded reasons."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())

    payload = health.build()

    assert payload is not None
    assert payload["message_type"] == "health"
    p = payload["payload"]
    assert p["status"] == "healthy"
    assert p["degraded_reasons"] == []
    assert p["wifi_connected"] is True
    assert p["mqtt_connected"] is True
    assert p["core_1_active"] is True
    assert p["network_stack_ready"] is True
    assert p["devices_configured"] == 1
    assert p["devices_active"] == 1
    assert p["outbound_queue_depth"] == 0
    assert p["outbound_queue_capacity"] == 16
    assert p["outbound_queued_bytes"] == 0
    # The low-watermark minimum tracks the lowest observed free heap and is
    # historical/diagnostic only; a single healthy sample leaves it at that sample.
    assert p["minimum_free_heap_observed_bytes"] == 100000
    assert p["utc_valid"] is True
    assert payload["uptime_ms"] == NOW_MS - BOOT_TICKS_MS  # 10000


def test_wifi_disconnected_triggers_degraded(health):
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_network({
        "wifi_connected": False, "mqtt_connected": True, "network_stack_ready": True,
    })

    payload = health.build()

    assert payload["payload"]["status"] == "degraded"
    assert "wifi_not_connected" in payload["payload"]["degraded_reasons"]


def test_mqtt_disconnected_triggers_degraded(health):
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_network({
        "wifi_connected": True, "mqtt_connected": False, "network_stack_ready": True,
    })

    payload = health.build()

    assert payload["payload"]["status"] == "degraded"
    assert "mqtt_not_connected" in payload["payload"]["degraded_reasons"]


def test_network_stack_not_ready_triggers_degraded(health):
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_network({
        "wifi_connected": True, "mqtt_connected": True, "network_stack_ready": False,
    })

    payload = health.build()

    assert payload["payload"]["status"] == "degraded"
    assert "network_stack_not_ready" in payload["payload"]["degraded_reasons"]


def test_core_1_inactive_triggers_degraded(health):
    """A stale Core-1 activity stamp (beyond the threshold) degrades health."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_core_1_activity(40000)  # 70000ms ago > 60000ms threshold

    payload = health.build()

    assert payload["payload"]["status"] == "degraded"
    assert "core_1_inactive" in payload["payload"]["degraded_reasons"]


def test_queue_pressure_triggers_degraded(health):
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    # 12 of 16 = 75% -> at the pressure threshold.
    for i in range(12):
        health.bus.outbound_queue.put_with_kind(
            KIND_HEALTH, json.dumps({"id": i}).encode(), RETENTION_PRIORITY_HEALTH
        )

    payload = health.build()

    assert payload["payload"]["status"] == "degraded"
    assert "outbound_queue_pressure" in payload["payload"]["degraded_reasons"]


def test_low_watermark_reports_lowest_sample_without_degrading(health):
    """minimum_free_heap_observed_bytes is the lowest observed free heap.

    The builder submits every sample to the shared MemoryStats, so the field
    tracks the lowest value seen since boot even after the heap recovers. It
    is historical/diagnostic: a low *past* minimum must never by itself add a
    degraded reason (only a *current* free_heap below the reserve does).
    """
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())

    # First sample dips below the reserve (low_free_heap applies now), then
    # recovers above it. The minimum must keep the low sample.
    health.set_free_heap(40000)
    low = health.build()["payload"]
    assert low["minimum_free_heap_observed_bytes"] == 40000
    assert "low_free_heap" in low["degraded_reasons"]

    health.set_free_heap(200000)  # recovered above the 65536 reserve
    recovered = health.build()["payload"]
    assert recovered["free_heap_bytes"] == 200000
    assert recovered["minimum_free_heap_observed_bytes"] == 40000  # still the low sample
    # Current heap is healthy -> no low_free_heap, and nothing else degrades it.
    assert "low_free_heap" not in recovered["degraded_reasons"]
    assert recovered["status"] == "healthy"


def test_utc_invalid_triggers_degraded(health):
    """A missing UTC snapshot degrades health with utc_not_valid."""
    health.set_healthy_baseline()
    # UTC is intentionally left unset (None) -> invalid.

    payload = health.build()

    assert payload["payload"]["status"] == "degraded"
    assert "utc_not_valid" in payload["payload"]["degraded_reasons"]


def test_low_free_heap_triggers_degraded(health):
    """Free heap below the board reserve degrades health with low_free_heap."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_free_heap(40000)  # below the 65536 reserve

    payload = health.build()

    assert payload["payload"]["status"] == "degraded"
    assert "low_free_heap" in payload["payload"]["degraded_reasons"]


def test_multiple_degradation_reasons(health):
    """Multiple failing conditions yield multiple degradation reasons."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_network({
        "wifi_connected": False, "mqtt_connected": True, "network_stack_ready": False,
    })
    for i in range(12):
        health.bus.outbound_queue.put_with_kind(
            KIND_HEALTH, json.dumps({"id": i}).encode(), RETENTION_PRIORITY_HEALTH
        )

    payload = health.build()

    reasons = payload["payload"]["degraded_reasons"]
    assert payload["payload"]["status"] == "degraded"
    assert "network_stack_not_ready" in reasons
    assert "wifi_not_connected" in reasons
    assert "outbound_queue_pressure" in reasons
    assert len(reasons) >= 3


# ---------------------------------------------------------------------------
# Runtime / uptime values and payload structure
# ---------------------------------------------------------------------------

def test_runtime_and_uptime_values(health):
    health.set_healthy_baseline()
    snapshot = _valid_utc_snapshot()
    health.set_utc(snapshot)
    health.set_clock(NOW_MS + 25000)  # 35s of uptime

    payload = health.build()

    assert payload["message_type"] == "health"
    assert payload["uptime_ms"] == (NOW_MS + 25000) - BOOT_TICKS_MS  # 35000
    # The timestamp is Core 1's: it is computed from the shared UTC snapshot,
    # advanced by the elapsed local ticks (the snapshot was taken 10s before
    # the clock at set_utc time, and the clock is now 25s past "now").
    expected_epoch_ms = snapshot["utc_epoch_ms"] + (NOW_MS + 25000) - snapshot["ticks_ms"]
    assert payload["timestamp"] == format_utc_epoch_ms(expected_epoch_ms)
    # The envelope keys are Core 0's: Core 1 must not carry them, or the wire
    # document would repeat a member name when Core 0 splices them in.
    for key in ("sequence", "runtime_id", "source", "firmware_version",
                "message_schema_version"):
        assert key not in payload


def test_payload_structure_matches_spec(health):
    """The health payload carries every field the schema specifies."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())

    payload = health.build()

    # Top level is Core 1's message fields only; the envelope
    # (sequence, runtime_id, source, firmware_version,
    # message_schema_version) is injected by Core 0 at publish time.
    for field in ("message_type", "uptime_ms", "timestamp", "payload"):
        assert field in payload
    for key in ("sequence", "runtime_id", "source", "firmware_version",
                "message_schema_version"):
        assert key not in payload

    p = payload["payload"]
    for field in (
        "status", "degraded_reasons", "hardware_type", "machine",
        "last_reset_cause",
        "network_stack_ready", "wifi_connected", "wifi_rssi_dbm", "mqtt_connected",
        "core_1_active", "core_1_activity_age_ms", "free_heap_bytes",
        "minimum_free_heap_bytes", "heap_headroom_bytes", "devices_configured",
        "devices_active", "device_failures", "outbound_queue_depth",
        "outbound_queue_capacity", "outbound_queue_utilization_percent",
        "outbound_queued_bytes",
        "outbound_queue_drain_active",
        "outbound_queue_last_drain_start_depth",
        "outbound_queue_last_drain_message_count",
        "outbound_queue_last_drain_duration_ms",
        "outbound_queue_last_drain_rate_per_sec",
        "minimum_free_heap_observed_bytes",
        "utc_valid", "utc_sync_age_sec",
        "mqtt_publish_attempt_count", "mqtt_publish_retry_count",
        "mqtt_puback_timeout_count", "mqtt_connection_failure_count",
        "mqtt_reconnect_success_count", "mqtt_last_reconnect_duration_ms",
        "mqtt_last_outage_duration_ms",
        "wifi_rssi_min_dbm", "wifi_rssi_max_dbm", "wifi_rssi_moving_average_dbm",
        "wifi_last_reconnect_duration_ms", "wifi_last_dhcp_acquisition_duration_ms",
        "gateway_reachable", "dns_reachable",
        "mqtt_broker_last_round_trip_ms", "wifi_bssid", "wifi_channel",
    ):
        assert field in p


def test_payload_is_json_safe(health):
    """The health payload serializes and validates cleanly."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())

    payload = health.build()

    payload_bytes = serialize_and_validate_message(payload)
    assert isinstance(payload_bytes, bytes)

    reconstructed = json.loads(payload_bytes.decode("utf-8"))
    assert reconstructed["message_type"] == "health"
    assert reconstructed["payload"]["status"] == "healthy"


def test_queue_depth_calculation():
    """Outbound queue depth counts both queued and in-flight entries."""
    bus = InterCore(outbound_max=16, event_max=4)

    depth, capacity = bus.outbound_queue.get_depth_with_capacity()
    assert depth == 0
    assert capacity == 16

    assert bus.outbound_queue.put_with_kind(
        KIND_HEALTH, json.dumps({"id": 1}).encode(), RETENTION_PRIORITY_HEALTH
    )
    depth, _ = bus.outbound_queue.get_depth_with_capacity()
    assert depth == 1

    first = bus.outbound_queue.take()
    assert first is not None
    depth, _ = bus.outbound_queue.get_depth_with_capacity()
    assert depth == 1  # in-flight still counts

    bus.outbound_queue.complete_in_flight(first)
    depth, _ = bus.outbound_queue.get_depth_with_capacity()
    assert depth == 0


# ---------------------------------------------------------------------------
# Conditional / extended fields
# ---------------------------------------------------------------------------

def test_hardware_type_and_machine_fields(health):
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_hardware({
        "hardware_type": "pico_2_w",
        "machine": "Raspberry Pi Pico 2 W with RP2350",
        "minimum_free_heap_bytes": 131072,
    })
    health.set_free_heap(200000)  # above the Pico 2 W reserve

    payload = health.build()

    p = payload["payload"]
    assert p["hardware_type"] == "pico_2_w"
    assert p["machine"] == "Raspberry Pi Pico 2 W with RP2350"
    assert p["minimum_free_heap_bytes"] == 131072
    assert p["status"] == "healthy"


def test_watchdog_reset_cause_is_reported_without_degrading_health(health):
    """A historical watchdog_reset is reported but never degrades health.

    last_reset_cause describes the boot event that began the current runtime;
    it must appear in the payload exactly as captured, while the current
    classification stays healthy with no degraded reasons.
    """
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_hardware({
        "hardware_type": "pico_w",
        "machine": "Raspberry Pi Pico W with RP2040",
        "minimum_free_heap_bytes": 65536,
        "last_reset_cause": "watchdog_reset",
    })

    payload = health.build()

    p = payload["payload"]
    assert p["last_reset_cause"] == "watchdog_reset"
    assert p["status"] == "healthy"
    assert p["degraded_reasons"] == []


def test_reset_cause_defaults_to_unknown_when_absent(health):
    """A hardware snapshot without last_reset_cause reports "unknown"."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_hardware({
        "hardware_type": "pico_w",
        "machine": "Raspberry Pi Pico W with RP2040",
        "minimum_free_heap_bytes": 65536,
    })

    payload = health.build()

    p = payload["payload"]
    assert p["last_reset_cause"] == "unknown"
    assert p["status"] == "healthy"


def test_mqtt_reliability_metrics_reported_without_degrading_health(health):
    """The seven MQTT reliability metrics are carried into the health payload
    from the Core 0 network snapshot, and historical failures never add a
    degraded reason: they describe what happened, not current liveness.

    A device that is fully healthy right now but has a rich failure history
    (PUBACK timeouts, connection failures, reconnects, outages) must still
    report healthy with no degraded reasons -- and none of the forbidden
    historical-failure reasons may appear.
    """
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_network({
        "wifi_connected": True,
        "mqtt_connected": True,
        "network_stack_ready": True,
        "rssi": -50,
        "mqtt_publish_attempt_count": 10,
        "mqtt_publish_retry_count": 3,
        "mqtt_puback_timeout_count": 5,      # historical failure
        "mqtt_connection_failure_count": 8,  # historical failure
        "mqtt_reconnect_success_count": 3,
        "mqtt_last_reconnect_duration_ms": 12000,
        "mqtt_last_outage_duration_ms": 16000,
    })

    payload = health.build()
    p = payload["payload"]

    assert p["mqtt_publish_attempt_count"] == 10
    assert p["mqtt_publish_retry_count"] == 3
    assert p["mqtt_puback_timeout_count"] == 5
    assert p["mqtt_connection_failure_count"] == 8
    assert p["mqtt_reconnect_success_count"] == 3
    assert p["mqtt_last_reconnect_duration_ms"] == 12000
    assert p["mqtt_last_outage_duration_ms"] == 16000

    # Historical failures must not degrade current health: the link is up and
    # every current-state rule is satisfied, so no degraded reasons at all.
    assert p["status"] == "healthy"
    assert p["degraded_reasons"] == []
    for reason in ("mqtt_publish_retry", "mqtt_puback_timeout",
                   "mqtt_connection_failure", "mqtt_previous_outage"):
        assert reason not in p["degraded_reasons"]


def test_mqtt_reliability_metrics_default_to_zero(health):
    """A network snapshot without the metric fields reports integer zero,
    never null."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    # set_healthy_baseline's network snapshot carries no mqtt_* metric fields.

    payload = health.build()
    p = payload["payload"]

    for field in (
        "mqtt_publish_attempt_count",
        "mqtt_publish_retry_count",
        "mqtt_puback_timeout_count",
        "mqtt_connection_failure_count",
        "mqtt_reconnect_success_count",
        "mqtt_last_reconnect_duration_ms",
        "mqtt_last_outage_duration_ms",
    ):
        assert p[field] == 0
        assert isinstance(p[field], int)
        assert p[field] is not None


def test_post_outage_drain_metrics_reported_without_degrading_health(health):
    """The five post-outage drain metrics are carried into the health payload
    from the Core 0 network snapshot (Core 0 is the single source), and a
    historically slow drain never adds a degraded reason: the current-state
    rules remain the only source of degraded reasons."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_network({
        "wifi_connected": True,
        "mqtt_connected": True,
        "network_stack_ready": True,
        "rssi": -50,
        "outbound_queue_drain_active": False,
        "outbound_queue_last_drain_start_depth": 64,
        "outbound_queue_last_drain_message_count": 64,
        "outbound_queue_last_drain_duration_ms": 30000,  # a slow past drain
        "outbound_queue_last_drain_rate_per_sec": 2,     # well under 5/s
    })

    payload = health.build()
    p = payload["payload"]

    assert p["outbound_queue_drain_active"] is False
    assert p["outbound_queue_last_drain_start_depth"] == 64
    assert p["outbound_queue_last_drain_message_count"] == 64
    assert p["outbound_queue_last_drain_duration_ms"] == 30000
    assert p["outbound_queue_last_drain_rate_per_sec"] == 2

    # A past slow drain is historical/diagnostic: the device is fully healthy
    # right now, so no degraded reasons at all.
    assert p["status"] == "healthy"
    assert p["degraded_reasons"] == []
    for reason in ("outbound_queue_drain_slow", "outbound_queue_drain",
                   "drain_rate_low", "outbound_queue_pressure"):
        assert reason not in p["degraded_reasons"]


def test_post_outage_drain_metrics_default_to_safe_values(health):
    """A network snapshot without the drain fields reports false/0 (never
    null) before the first completed drain."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    # set_healthy_baseline's network snapshot carries no drain fields.

    payload = health.build()
    p = payload["payload"]

    assert p["outbound_queue_drain_active"] is False
    assert p["outbound_queue_last_drain_start_depth"] == 0
    assert p["outbound_queue_last_drain_message_count"] == 0
    assert p["outbound_queue_last_drain_duration_ms"] == 0
    assert p["outbound_queue_last_drain_rate_per_sec"] == 0
    for field in (
        "outbound_queue_last_drain_start_depth",
        "outbound_queue_last_drain_message_count",
        "outbound_queue_last_drain_duration_ms",
        "outbound_queue_last_drain_rate_per_sec",
    ):
        assert isinstance(p[field], int)
        assert p[field] is not None


def test_network_diagnostics_reported_without_degrading_health(health):
    """The network-quality and diagnostics fields are carried into the health
    payload from the Core 0 network snapshot, and a weak/failed history never
    adds a degraded reason: they describe what happened, not current liveness.

    A device that is fully healthy right now but has a rough link history
    (weak RSSI floor, slow DHCP, gateway and DNS probes failing, a slow
    broker round trip) must still report healthy with no degraded reasons --
    and none of the forbidden historical-diagnostics reasons may appear.
    """
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_network({
        "wifi_connected": True,
        "mqtt_connected": True,
        "network_stack_ready": True,
        "rssi": -50,
        "wifi_rssi_min_dbm": -90,                 # weak historical floor
        "wifi_rssi_max_dbm": -42,
        "wifi_rssi_moving_average_dbm": -63,
        "wifi_last_reconnect_duration_ms": 8000,
        "wifi_last_dhcp_acquisition_duration_ms": 4200,
        "gateway_reachable": False,               # probed and failed
        "dns_reachable": False,                   # probed and failed
        "mqtt_broker_last_round_trip_ms": 900,
        "wifi_bssid": None,
        "wifi_channel": None,
    })

    payload = health.build()
    p = payload["payload"]

    assert p["wifi_rssi_min_dbm"] == -90
    assert p["wifi_rssi_max_dbm"] == -42
    assert p["wifi_rssi_moving_average_dbm"] == -63
    assert p["wifi_last_reconnect_duration_ms"] == 8000
    assert p["wifi_last_dhcp_acquisition_duration_ms"] == 4200
    assert p["gateway_reachable"] is False
    assert p["dns_reachable"] is False
    assert p["mqtt_broker_last_round_trip_ms"] == 900
    assert p["wifi_bssid"] is None
    assert p["wifi_channel"] is None

    # A failed probe or weak RSSI history must not degrade current health:
    # the link is up and every current-state rule is satisfied.
    assert p["status"] == "healthy"
    assert p["degraded_reasons"] == []
    for reason in ("gateway_unreachable", "dns_unreachable", "weak_rssi",
                   "slow_dhcp", "high_broker_latency"):
        assert reason not in p["degraded_reasons"]


def test_wifi_rssi_dbm_field(health):
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_network({
        "wifi_connected": True, "mqtt_connected": True,
        "network_stack_ready": True, "rssi": -34,
    })

    payload = health.build()

    assert payload["payload"]["wifi_rssi_dbm"] == -34


def test_wifi_rssi_dbm_null_when_unavailable(health):
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_network({
        "wifi_connected": True, "mqtt_connected": True, "network_stack_ready": True,
    })  # no rssi key

    payload = health.build()

    assert payload["payload"]["wifi_rssi_dbm"] is None


def test_heap_headroom_bytes_field(health):
    """heap_headroom_bytes = free_heap - minimum_free_heap."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_free_heap(100000)
    health.set_hardware({
        "hardware_type": "pico_w", "machine": "Pico W", "minimum_free_heap_bytes": 65536,
    })

    payload = health.build()

    assert payload["payload"]["free_heap_bytes"] == 100000
    assert payload["payload"]["heap_headroom_bytes"] == 100000 - 65536  # 34464


def test_heap_headroom_negative_when_low(health):
    """heap_headroom_bytes is negative when free heap is below the reserve."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_free_heap(100000)
    health.set_hardware({
        "hardware_type": "pico_w", "machine": "Pico W", "minimum_free_heap_bytes": 120000,
    })

    payload = health.build()

    assert payload["payload"]["heap_headroom_bytes"] == 100000 - 120000  # -20000


def test_core_1_activity_age_ms_field(health):
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_core_1_activity(NOW_MS - 43)  # 43ms ago

    payload = health.build()

    assert payload["payload"]["core_1_activity_age_ms"] == 43
    assert payload["payload"]["core_1_active"] is True


def test_utc_sync_age_sec_field(health):
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot(age_ms=10000))  # 10s ago

    payload = health.build()

    assert payload["payload"]["utc_valid"] is True
    assert payload["payload"]["utc_sync_age_sec"] == 10


def test_utc_sync_age_null_when_not_synchronized(health):
    health.set_healthy_baseline()
    # UTC intentionally unset -> invalid, no age.

    payload = health.build()

    assert payload["payload"]["utc_valid"] is False
    assert payload["payload"]["utc_sync_age_sec"] is None


def test_device_state_and_failures(health):
    """device_failures = devices_configured - devices_active."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    health.set_devices(1, 0)  # 0 active of 1 configured

    payload = health.build()

    p = payload["payload"]
    assert p["devices_configured"] == 1
    assert p["devices_active"] == 0
    assert p["device_failures"] == 1
    assert "device_count_mismatch" in p["degraded_reasons"]


def test_outbound_queue_utilization_percent_field(health):
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())
    for i in range(12):  # 12 of 16 = 75%
        health.bus.outbound_queue.put_with_kind(
            KIND_HEALTH, json.dumps({"id": i}).encode(), RETENTION_PRIORITY_HEALTH
        )

    payload = health.build()

    p = payload["payload"]
    assert p["outbound_queue_depth"] == 12
    assert p["outbound_queue_capacity"] == 16
    assert p["outbound_queue_utilization_percent"] == 75


def test_existing_classification_unchanged(health):
    """The extended fields do not alter the healthy/degraded classification."""
    health.set_healthy_baseline()
    health.set_utc(_valid_utc_snapshot())

    payload = health.build()
    assert payload["payload"]["status"] == "healthy"
    assert payload["payload"]["degraded_reasons"] == []

    health.set_network({
        "wifi_connected": False, "mqtt_connected": True, "network_stack_ready": True,
    })
    payload = health.build()
    assert payload["payload"]["status"] == "degraded"
    assert "wifi_not_connected" in payload["payload"]["degraded_reasons"]
