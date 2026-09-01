# test_utc_sync.py - Tests for non-blocking UTC synchronization in Core 0
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import importlib
import json
import pathlib
import sys
import time as _real_time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeTime:
    """Controllable stand-in for MicroPython's time module.

    sleep_ms advances the clock so bounded wait loops terminate deterministically in tests."""

    def __init__(self):
        self.now_ms = 0
        self.sleep_calls = []

    def ticks_ms(self):
        return self.now_ms

    def ticks_diff(self, now, prev):
        return now - prev

    def ticks_add(self, base, delta):
        return base + delta

    def sleep_ms(self, ms):
        self.sleep_calls.append(ms)
        self.now_ms += ms

    def sleep(self, secs):
        self.sleep_ms(int(secs * 1000))

    def gmtime(self, secs):
        return _real_time.gmtime(secs)

    def __getattr__(self, name):
        # Anything not explicitly faked falls through to the real time
        # module (perf_counter, monotonic, ...) so host tooling keeps working.
        return getattr(_real_time, name)


_FAKE_TIME = FakeTime()

# MicroPython stand-ins for importing core0 on the host. Other test modules
# (e.g. tests/test_core0_recovery.py) also import core0 with their own
# stand-ins, so the mocks are re-installed and core0 is reloaded inside the
# fixture before each instance is built: this module's mocks are
# authoritative at test time regardless of collection order.
_MACHINE_MOCK = MagicMock()
_DEBUG_MOCK = MagicMock()
_DEBUG_MOCK.DEBUG = False
_WIFI_MOCK = MagicMock()
_MQTT_MOCK = MagicMock()


def _install_mocks():
    sys.modules["time"] = _FAKE_TIME
    sys.modules["machine"] = _MACHINE_MOCK
    sys.modules["debug"] = _DEBUG_MOCK
    sys.modules["wifi"] = _WIFI_MOCK
    sys.modules["mqtt"] = _MQTT_MOCK


_install_mocks()

import core0 as core0_module  # noqa: E402
from config import split_config  # noqa: E402
from config_manager import ConfigManager  # noqa: E402
from version import MESSAGE_SCHEMA_VERSION  # noqa: E402

_MQTT_INSTANCE = _MQTT_MOCK.Mqtt.return_value


def _load_core0_config(broker_response_timeout_sec):
    config = json.loads((ROOT / "config.json").read_text())
    config["mqtt_broker_response_timeout_sec"] = broker_response_timeout_sec
    core0_config, _core1_config= split_config(config)
    return core0_config


class MockStateMailboxes:
    def __init__(self):
        self.utc_snapshots = []

    def set_utc_snapshot(self, snapshot):
        self.utc_snapshots.append(snapshot)

    def get_utc_snapshot(self):
        if self.utc_snapshots:
            return self.utc_snapshots[-1]
        return None


class MockInterCore:
    def __init__(self):
        self.state_mailboxes = MockStateMailboxes()
        self.outbound_queue = MagicMock()
        self.event_queue = MagicMock()


@pytest.fixture
def make_core0():
    """Build a fresh Core0 with a controllable clock and mocked MQTT."""
    def _make(broker_response_timeout_sec=4):
        _FAKE_TIME.now_ms = 0
        _FAKE_TIME.sleep_calls.clear()
        _MQTT_INSTANCE.reset_mock()
        _MQTT_INSTANCE.is_connected.return_value = True
        _install_mocks()
        # core0 imports uptime; rebind its time to the fake before reloading core0.
        importlib.reload(importlib.import_module("uptime"))
        importlib.reload(core0_module)
        return core0_module.Core0(
            MockInterCore(),
            _load_core0_config(broker_response_timeout_sec),
            {"wifi_ssid": "test-ssid", "wifi_password": "test-password"},
            "test-runtime",
            0,
            MagicMock(),
                    ConfigManager("config.json"),
        )

    return _make


def _info_response_doc(instance, request_id, payload):
    return {
        "message_type": "info_response",
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "source": "server",
        "target": instance._config["source"],
        "request_type": "utc_time",
        "request_id": request_id,
        "payload": payload,
    }


def test_startup_attempt_count_not_coupled_to_timeout(make_core0):
    """The startup retry count must be a fixed constant, not the timeout value."""
    expected = core0_module._UTC_STARTUP_MAX_ATTEMPTS
    for timeout_sec in (1, 8):
        instance = make_core0(timeout_sec)
        assert instance._synchronize_utc_required() is False
        assert _MQTT_INSTANCE.publish_qos1.call_count == expected, (
            "attempts={}".format(_MQTT_INSTANCE.publish_qos1.call_count)
            + " with timeout={}s".format(timeout_sec)
        )


def test_startup_succeeds_when_response_arrives(make_core0):
    instance = make_core0(4)
    epoch_ms = 1767225600000

    def deliver_response():
        doc = _info_response_doc(
            instance, instance._pending_utc_request_id,
            {"timestamp": "2026-01-01T00:00:00Z", "utc_epoch_ms": epoch_ms},
        )
        instance._handle_info_response(doc)

    _MQTT_INSTANCE.check_msg.side_effect = deliver_response
    assert instance._synchronize_utc_required() is True
    assert instance._utc_snapshot is not None
    assert instance._utc_snapshot["utc_epoch_ms"] == epoch_ms
    assert instance._pending_utc_request_id is None


def test_utc_send_request_is_nonblocking(make_core0):
    instance = make_core0(4)
    _MQTT_INSTANCE.reset_mock()

    instance._utc_send_request()

    assert instance._pending_utc_request_id is not None
    assert instance._utc_last_attempt_ms is not None
    assert instance._utc_request_deadline_ms == (
        instance._utc_last_attempt_ms + 4 * 1000
    )
    # exactly one broker request, no pumping, no sleeping
    assert _MQTT_INSTANCE.publish_qos1.call_count == 1
    assert _MQTT_INSTANCE.check_msg.call_count == 0
    assert _FAKE_TIME.sleep_calls == []


def test_utc_request_expires_at_deadline(make_core0):
    instance = make_core0(4)
    instance._utc_send_request()
    pending = instance._pending_utc_request_id

    _FAKE_TIME.now_ms = 4 * 1000 - 1
    instance._utc_request_expired()
    assert instance._pending_utc_request_id == pending

    _FAKE_TIME.now_ms = 4 * 1000
    instance._utc_request_expired()
    assert instance._pending_utc_request_id is None
    assert instance._utc_request_deadline_ms is None


def test_retry_is_throttled_until_interval_elapses(make_core0):
    instance = make_core0(4)
    interval = core0_module._UTC_RETRY_INTERVAL_MS

    # Never attempted: allowed (sync is due, no snapshot yet).
    _FAKE_TIME.now_ms = 0
    assert instance._utc_should_send_request() is True

    # Attempted 1 second ago: throttled.
    _FAKE_TIME.now_ms = 1000
    instance._utc_last_attempt_ms = 0
    assert instance._utc_should_send_request() is False

    # Attempted just past the interval: allowed again.
    _FAKE_TIME.now_ms = 1000 + interval
    assert instance._utc_should_send_request() is True


def test_no_retry_while_snapshot_is_fresh(make_core0):
    instance = make_core0(4)
    interval_ms = instance._config["datetime_sync_interval_min"] * 60 * 1000

    _FAKE_TIME.now_ms = interval_ms - 1000
    instance._utc_snapshot = {
        "ticks_ms": 0,
        "utc_epoch_ms": 1767225600000,
    }
    assert instance._utc_sync_due() is False
    assert instance._utc_should_send_request() is False


def test_malformed_response_clears_pending_request(make_core0):
    """A malformed answer to our own request must free the slot for a retry."""
    instance = make_core0(4)
    bad_payloads = (
        "not-a-dict",
        {"timestamp": ""},
        {"timestamp": "2026-01-01T00:00:00Z", "utc_epoch_ms": -1},
    )
    for bad_payload in bad_payloads:
        instance._utc_send_request()
        doc = _info_response_doc(
            instance, instance._pending_utc_request_id, bad_payload
        )
        instance._on_mqtt_message(
            instance._config["mqtt_topic_info_response"], json.dumps(doc)
        )
        assert instance._pending_utc_request_id is None
        assert instance._utc_request_deadline_ms is None
        assert instance._utc_snapshot is None


def test_malformed_response_enables_prompt_retry(make_core0):
    """A malformed answer to our own request must not eat the 30s throttle."""
    instance = make_core0(4)
    instance._utc_send_request()
    doc = _info_response_doc(
        instance, instance._pending_utc_request_id, {"timestamp": ""}
    )
    instance._handle_info_response(doc)

    # Not unthrottled: the short backoff still applies.
    _FAKE_TIME.now_ms = 100
    assert instance._utc_should_send_request() is False

    # Prompt: allowed well before the 30s retry interval.
    _FAKE_TIME.now_ms = 600
    assert instance._utc_should_send_request() is True


def test_prompt_retry_backoff_reapplies_after_each_malformed_response(make_core0):
    """Each malformed answer re-arms the short backoff, never the long one."""
    instance = make_core0(4)
    instance._utc_send_request()
    instance._handle_info_response(
        _info_response_doc(
            instance, instance._pending_utc_request_id, {"timestamp": ""}
        )
    )
    _FAKE_TIME.now_ms = 600
    assert instance._utc_should_send_request() is True

    instance._utc_send_request()
    instance._handle_info_response(
        _info_response_doc(
            instance, instance._pending_utc_request_id, {"timestamp": ""}
        )
    )

    # Not unthrottled: the short backoff applies again.
    _FAKE_TIME.now_ms = 700
    assert instance._utc_should_send_request() is False

    # And still well before the 30s retry interval.
    _FAKE_TIME.now_ms = 1200
    assert instance._utc_should_send_request() is True


def test_timeout_path_keeps_full_retry_interval(make_core0):
    """A silent server must keep the full 30s throttle, not the short backoff."""
    instance = make_core0(4)
    instance._utc_send_request()

    _FAKE_TIME.now_ms = 4 * 1000
    instance._utc_request_expired()
    assert instance._pending_utc_request_id is None

    # 10s since send: still throttled (the short backoff must not apply here).
    _FAKE_TIME.now_ms = 10 * 1000
    assert instance._utc_should_send_request() is False

    # 30s since send: allowed again.
    _FAKE_TIME.now_ms = 30 * 1000
    assert instance._utc_should_send_request() is True


def test_response_for_other_request_preserves_pending(make_core0):
    """A response we did not ask for must not cancel our in-flight request."""
    instance = make_core0(4)
    instance._utc_send_request()
    pending = instance._pending_utc_request_id

    doc = _info_response_doc(
        instance, "some_other_runtime_42",
        {"timestamp": "2026-01-01T00:00:00Z", "utc_epoch_ms": 1767225600000},
    )
    instance._on_mqtt_message(
        instance._config["mqtt_topic_info_response"], json.dumps(doc)
    )
    assert instance._pending_utc_request_id == pending
    assert instance._utc_snapshot is None


def test_valid_response_completes_sync(make_core0):
    instance = make_core0(4)
    instance._utc_send_request()
    epoch_ms = 1767225600000

    doc = _info_response_doc(
        instance, instance._pending_utc_request_id,
        {"timestamp": "2026-01-01T00:00:00Z", "utc_epoch_ms": epoch_ms},
    )
    instance._on_mqtt_message(
        instance._config["mqtt_topic_info_response"], json.dumps(doc)
    )

    assert instance._utc_snapshot is not None
    assert instance._utc_snapshot["utc_epoch_ms"] == epoch_ms
    assert instance._pending_utc_request_id is None
    assert instance._utc_request_deadline_ms is None
    assert len(instance._intercore.state_mailboxes.utc_snapshots) == 1


def test_fast_response_delivered_during_publish_is_accepted(make_core0):
    """A response that overtakes the PUBACK must not be dropped.

    MQTTClient.publish delivers broker messages while awaiting the PUBACK, so a fast UTC response can arrive inside the publish call itself. The pending request ID must be armed before publishing; otherwise every startup attempt loses its response and start() fails."""
    instance = make_core0(4)
    epoch_ms = 1767225600000

    def publish_and_respond(topic, message):
        # The broker answers the request before it acks it: the response
        # arrives through the message callback inside the publish call.
        request = json.loads(message)
        doc = _info_response_doc(
            instance, request["request_id"],
            {"timestamp": "2026-01-01T00:00:00Z", "utc_epoch_ms": epoch_ms},
        )
        instance._on_mqtt_message(
            instance._config["mqtt_topic_info_response"], json.dumps(doc)
        )

    _MQTT_INSTANCE.publish_qos1.side_effect = publish_and_respond

    assert instance._synchronize_utc_required() is True
    assert instance._utc_snapshot is not None
    assert instance._utc_snapshot["utc_epoch_ms"] == epoch_ms
    assert instance._pending_utc_request_id is None


def test_failed_publish_rolls_back_armed_request(make_core0):
    """A failed publish must clear the armed request ID so a retry can arm.

    Without the rollback the pending ID would stay set with no deadline: _utc_should_send_request() would then refuse to retry forever."""
    instance = make_core0(4)
    _MQTT_INSTANCE.publish_qos1.side_effect = OSError("PUBACK timeout")

    instance._utc_send_request()

    assert instance._pending_utc_request_id is None
    assert instance._utc_request_deadline_ms is None
    assert instance._utc_snapshot is None
    # The retry gate is open: the failed attempt left no armed state, so a
    # fresh request can be issued (recovery backoff paces the retry).
    assert instance._utc_should_send_request() is True
