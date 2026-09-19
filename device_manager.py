# device_manager.py - Device lifecycle management
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import time
from device_factory import create_device
from message_protocol import is_json_safe
from uptime import current_uptime_ms

# Device state constants
DEVICE_STATE_READY = "ready"
DEVICE_STATE_REINITIALIZE_PENDING = "reinitialize_pending"
DEVICE_STATE_INITIALIZATION_FAILED = "initialization_failed"

# Device result status constants
DEVICE_RESULT_TELEMETRY = "telemetry"
DEVICE_RESULT_READ_FAILED = "read_failed"
DEVICE_RESULT_REINITIALIZED = "reinitialized"
DEVICE_RESULT_REINITIALIZATION_FAILED = "reinitialization_failed"
DEVICE_RESULT_BOOT_RECOVERED = "boot_recovered"
DEVICE_RESULT_BOOT_RECOVERY_FAILED = "boot_recovery_failed"


class ManagedDevice:
    """Runtime wrapper around a successfully initialized device."""

    def __init__(self, device_id, device_type, driver, name=None):
        self.device_id = device_id
        self.device_type = device_type
        self.driver = driver
        self.name = name

        self.state = DEVICE_STATE_READY
        self.initialization_attempts_used = 0
        self.consecutive_read_failures = 0
        self.total_read_failures = 0
        self.read_count = 0
        self.successful_read_count = 0
        # Read timestamps on the shared boot-relative uptime base.
        self.last_read_ms = None
        self.last_successful_read_ms = None
        self.reinitialize_pending = False
        self.reinit_failure_logged = False  # Suppress duplicate ERROR logs

    def mark_reinitialize_pending(self):
        self.reinitialize_pending = True
        self.state = DEVICE_STATE_REINITIALIZE_PENDING

    def clear_reinitialize_pending(self):
        self.reinitialize_pending = False
        self.state = DEVICE_STATE_READY
        self.consecutive_read_failures = 0
        self.reinit_failure_logged = False  # Reset suppression flag on success

    def set_reinit_failure_logged(self):
        self.reinit_failure_logged = True

    def should_suppress_reinit_failure(self):
        return self.reinit_failure_logged

    def record_read_failure(self):
        self.consecutive_read_failures += 1
        self.total_read_failures += 1

    def record_read_success(self):
        """Record a successful read, resetting consecutive failures."""
        self.consecutive_read_failures = 0

    def get_status_snapshot(self, now_ms=None):
        """Get a JSON-safe status snapshot of this device (age fields when now_ms is given)."""
        snapshot = {
            "id": self.device_id,
            "device": self.device_type,
            "state": self.state,
            "initialization_attempts_used": self.initialization_attempts_used,
            "consecutive_read_failures": self.consecutive_read_failures,
            "total_read_failures": self.total_read_failures,
            "read_count": self.read_count,
            "successful_read_count": self.successful_read_count,
        }

        if self.name is not None:
            snapshot["name"] = self.name

        # Age fields: now_ms and the stored timestamps share the boot-relative
        # uptime base, so the age is a plain subtraction (ticks_diff would wrap
        # past half a tick period).
        if now_ms is not None:
            snapshot["last_read_age_ms"] = None
            snapshot["last_successful_read_age_ms"] = None
            if self.last_read_ms is not None:
                snapshot["last_read_age_ms"] = now_ms - self.last_read_ms
            if self.last_successful_read_ms is not None:
                snapshot["last_successful_read_age_ms"] = now_ms - self.last_successful_read_ms

        return snapshot


class DeviceManager:
    """Manages device lifecycle for Core 1.

    Operational failure domain: only ``OSError`` is a recoverable device
    failure. Every driver normalizes its hardware/I2C failures to it (a bus
    drop, a sensor timeout, a missing chip), so each recovery path -- driver
    construction, boot initialization, the normal read, the runtime
    reinitialization, and the boot-failure late-retry pass -- catches
    ``OSError`` beside the ``MemoryError`` re-raise. Anything else is a
    firmware defect that reinitializing cannot repair: a ``TypeError``
    contract violation (including the manager's own check of a ``read()``
    return), a ``ValueError`` from a driver that reached its validator
    despite the config-boundary validation or from the I2C bus factory's
    same-bus conflict, a ``RuntimeError``/``ArithmeticError`` state defect.
    Those escape to Core 1's worker boundary, where the dead worker is
    recovered by Core 0's heartbeat watchdog, instead of the fault being
    reclassified as a failed sensor and retried indefinitely into itself.
    """

    def __init__(self, config, activity_refresh=None, uptime_state=None, i2c_bus_factory=None):
        self._active_devices = []
        self._failed_devices = {}

        self._device_initialization_attempts = config["device_initialization_attempts"]
        self._device_initialization_retry_delay_ms = config["device_initialization_retry_delay_ms"]
        self._device_read_failure_threshold = config["device_read_failure_threshold"]
        self._devices_config = config["devices"]

        # Core 1's I2C bus factory (builds/dedupes machine.I2C per bus config).
        # Injected so this module stays host-importable; None for configs with
        # no I2C device, in which case create_device never asks for a bus.
        self._i2c_bus_factory = i2c_bus_factory

        # Core 1's accumulated-uptime state, the source of truth for the
        # read-age fields; raw ticks when not wired (host tests).
        self._uptime_state = uptime_state

        # Optional liveness-stamp refresh callback, invoked at progress
        # boundaries (per device, per attempt, per retry-sleep step). Without
        # it, a legitimately long initialization would age the stamp past
        # Core 0's watchdog; a wedge inside a driver call still stops the
        # refresh and is caught.
        self._activity_refresh = activity_refresh

    def _refresh_activity(self):
        """Refresh Core 1's liveness stamp at a progress boundary, if armed."""
        if self._activity_refresh is not None:
            self._activity_refresh()

    def _now_ms(self):
        """Time on the shared boot-relative uptime base (raw ticks when not
        wired). Stored read timestamps and read-age subtractions share this
        base, so ages stay correct past half a tick period."""
        if self._uptime_state is not None:
            return current_uptime_ms(self._uptime_state)
        return time.ticks_ms()

    # One liveness-refresh step inside a long retry sleep.
    _ACTIVITY_REFRESH_STEP_MS = 100

    def _sleep_with_activity_refresh(self, delay_ms):
        """Sleep in steps, refreshing the liveness stamp at each boundary: the
        retry delay is unbounded, so a monolithic sleep could age the stamp
        past Core 0's watchdog."""
        while delay_ms > 0:
            step_ms = min(delay_ms, self._ACTIVITY_REFRESH_STEP_MS)
            time.sleep_ms(step_ms)
            self._refresh_activity()
            delay_ms -= step_ms

    def _initialize_single_device(self, device_def):
        device_id = device_def["id"]
        device_type = device_def["device_type"]
        name = device_def.get("name")

        driver, driver_error = self._create_driver(device_def)
        if driver is None:
            return self._record_driver_failure(device_id, device_type, driver_error)

        attempts_used, initialized, last_error = self._initialize_driver_with_retries(
            driver, device_def
        )

        if not initialized:
            return self._record_initialization_failure(
                device_id, device_type, attempts_used, last_error
            )

        return self._record_success(
            device_id, device_type, driver, attempts_used, name=name
        )

    def _create_driver(self, device_def):
        try:
            if self._i2c_bus_factory is None:
                # No I2C bus factory wired (no I2C device configured): the
                # one-argument construction path.
                driver = create_device(device_def)
            else:
                driver = create_device(device_def, self._i2c_bus_factory)
            return driver, None
        except MemoryError:
            raise
        # Operational failure domain: OSError only. A factory ValueError
        # (unsupported type, the I2C bus same-bus conflict) or TypeError is
        # a configuration/programming error -- recording it as a failed
        # device would hide it behind the retry machinery.
        except OSError as err:
            return None, err

    def _initialize_driver_with_retries(self, driver, device_def):
        # Only the attempt count and final error are retained: the recovery
        # algorithm needs no per-attempt history.
        attempts_used = 0
        last_error = None
        device_config = device_def["config"]

        for attempt in range(1, self._device_initialization_attempts + 1):
            attempts_used = attempt
            # Progress boundary: covers the retry delay slept before this attempt.
            self._refresh_activity()
            try:
                driver.initialize(device_config)
                # Initialization succeeded (returns None, raises on failure)
                return attempts_used, True, None
            except MemoryError:
                raise
            # OSError (hardware/I2C) only: a validator ValueError or driver
            # state defect is deterministic -- the retry budget must not be
            # burned re-hitting the same fault.
            except OSError as err:
                last_error = str(err)
                if attempt < self._device_initialization_attempts:
                    self._sleep_with_activity_refresh(self._device_initialization_retry_delay_ms)

        return attempts_used, False, last_error

    def _record_driver_failure(self, device_id, device_type, error):
        last_error = str(error)
        self._failed_devices[device_id] = {
            "id": device_id,
            "device": device_type,
            "state": DEVICE_STATE_INITIALIZATION_FAILED,
            # The failure happened in create_device() before any initialize()
            # ran: 0 is the honest count (1 would report a phantom attempt).
            "initialization_attempts_used": 0,
            "failure_reason": last_error,
        }
        return {
            "success": False,
            "device_id": device_id,
            "device_type": device_type,
        }

    def _record_initialization_failure(self, device_id, device_type, attempts_used, last_error):
        self._failed_devices[device_id] = {
            "id": device_id,
            "device": device_type,
            "state": DEVICE_STATE_INITIALIZATION_FAILED,
            "initialization_attempts_used": attempts_used,
            "failure_reason": last_error,
        }
        return {
            "success": False,
            "device_id": device_id,
            "device_type": device_type,
        }

    def _record_success(self, device_id, device_type, driver, attempts_used, name=None):
        managed_device = ManagedDevice(
            device_id=device_id,
            device_type=device_type,
            driver=driver,
            name=name,
        )
        managed_device.initialization_attempts_used = attempts_used

        self._active_devices.append(managed_device)

        return {
            "success": True,
            "device_id": device_id,
            "device_type": device_type,
        }

    def initialize_devices(self):
        """Initialize all configured devices in configuration order, with the
        configured attempts and retry delay per device; on final failure record
        the device (in the failure status the system-information section
        reports) and continue. Returns the count initialized."""
        initialized_count = 0

        for device_def in self._devices_config:
            # Progress boundary: covers driver construction.
            self._refresh_activity()
            result = self._initialize_single_device(device_def)
            if result["success"]:
                initialized_count += 1

        return initialized_count

    def get_active_devices(self):
        """The active managed devices in configuration order. Returns the
        internal list, not a copy: membership is fixed after initialize_devices()
        and a per-cycle copy would be heap churn on the hot telemetry path."""
        return self._active_devices

    def process_device(self, managed_device):
        """Process one device for the current cycle (normal read, or
        reinitialization if pending). The stamp refreshes once per device so
        the watchdog measures one device operation, not the cumulative pass."""
        self._refresh_activity()
        if managed_device.reinitialize_pending:
            return self._process_reinitialization(managed_device)

        return self._process_normal_read(managed_device)

    def _process_normal_read(self, managed_device):
        """Process a normal read for a device; validate the telemetry and record the outcome."""
        try:
            managed_device.read_count += 1
            managed_device.last_read_ms = self._now_ms()

            telemetry = managed_device.driver.read()

            # Telemetry must be a non-empty, JSON-safe dictionary
            if not isinstance(telemetry, dict) or len(telemetry) == 0:
                raise TypeError("Driver read() must return a non-empty dictionary")
            if not is_json_safe(telemetry):
                raise TypeError("Driver read() returned non-JSON-safe data")

            managed_device.successful_read_count += 1
            managed_device.last_successful_read_ms = self._now_ms()

            previous_failures = managed_device.consecutive_read_failures
            managed_device.record_read_success()

            return {
                "status": DEVICE_RESULT_TELEMETRY,
                "device_id": managed_device.device_id,
                "device": managed_device.device_type,
                "name": managed_device.name,
                "telemetry": telemetry,
                "recovered": previous_failures > 0,
                "previous_consecutive_read_failures": previous_failures,
            }
        except MemoryError:
            raise
        # OSError (hardware/I2C) only: a read failure is a sensor condition,
        # so the driver's I2C errors are retried toward reinitialization.
        # The manager's own contract TypeError above and any driver defect
        # escape instead of counting as sensor failures.
        except OSError as err:
            managed_device.record_read_failure()

            if managed_device.consecutive_read_failures >= self._device_read_failure_threshold:
                managed_device.mark_reinitialize_pending()

            return {
                "status": DEVICE_RESULT_READ_FAILED,
                "device_id": managed_device.device_id,
                "device": managed_device.device_type,
                "error": err,
                "consecutive_read_failures": managed_device.consecutive_read_failures,
                "total_read_failures": managed_device.total_read_failures,
                "device_read_failure_threshold": self._device_read_failure_threshold,
                "reinitialize_pending": managed_device.reinitialize_pending,
            }

    def _process_reinitialization(self, managed_device):
        """Reinitialize a device, reusing the configured retry policy. A
        failed attempt does not remove the device: it stays
        reinitialize_pending for the next cycle."""
        device_def = None
        for d in self._devices_config:
            if d["id"] == managed_device.device_id:
                device_def = d
                break
        if device_def is None:
            raise RuntimeError("Device definition not found for reinitialization")

        last_error = None
        attempts_used = 0

        for attempt in range(1, self._device_initialization_attempts + 1):
            attempts_used = attempt
            # Progress boundary, the same strategy as the startup path.
            self._refresh_activity()
            try:
                managed_device.driver.initialize(device_def["config"])
                # Initialization succeeded (returns None, raises on failure)
                managed_device.clear_reinitialize_pending()

                return {
                    "status": DEVICE_RESULT_REINITIALIZED,
                    "device_id": managed_device.device_id,
                    "device": managed_device.device_type,
                    "reinitialization_attempts_used": attempts_used,
                }
            except MemoryError:
                raise
            # OSError (hardware/I2C) only, the same domain as the boot path:
            # a deterministic contract defect must not consume the reinit
            # attempts.
            except OSError as err:
                last_error = str(err)
                if attempt < self._device_initialization_attempts:
                    self._sleep_with_activity_refresh(self._device_initialization_retry_delay_ms)

        # All attempts exhausted: the device stays reinitialize_pending (not
        # removed) so transient failures are retried next cycle. Log the first
        # failure and suppress repeats until a successful reinit clears the flag.
        log_failure_warning = not managed_device.should_suppress_reinit_failure()
        if log_failure_warning:
            managed_device.set_reinit_failure_logged()

        return {
            "status": DEVICE_RESULT_REINITIALIZATION_FAILED,
            "device_id": managed_device.device_id,
            "device": managed_device.device_type,
            "error": last_error,
            "consecutive_read_failures": managed_device.consecutive_read_failures,
            "total_read_failures": managed_device.total_read_failures,
            "reinitialization_attempts_used": attempts_used,
            "log_failure_warning": log_failure_warning,
        }

    def has_failed_devices(self):
        """Whether any boot-failed device remains: the steady-state retry
        pass runs only while the set is non-empty, so the Core 1 loop's
        gate is a single bool check once everything has recovered."""
        return bool(self._failed_devices)

    def retry_failed_devices(self):
        """One steady-state retry pass over the boot-failed devices, in
        configuration order: exactly one fresh initialize() attempt each.
        The caller's slow interval is the retry policy, so the full
        attempts x delay boot loop is deliberately NOT re-run here -- a
        dead sensor must cost at most one bounded driver call, never hold
        the read pass and every working sensor's telemetry. A recovered
        device is promoted to a fresh READY ManagedDevice (cumulative
        attempt count, zero read-failure state) and leaves the failure
        record; a failed attempt updates the record (cumulative attempts,
        last error) so the next pass retries it. Returns one result per
        attempted device."""
        results = []

        for device_def in self._devices_config:
            device_id = device_def["id"]
            failed_info = self._failed_devices.get(device_id)
            if failed_info is None:
                continue

            # Progress boundary: the same strategy as the boot path, so a
            # legitimately slow probe cannot age the liveness stamp.
            self._refresh_activity()

            driver, driver_error = self._create_driver(device_def)
            if driver is None:
                # No initialize() ran: the attempt count stays honest
                # (the boot path's 0 rule) and the constructor error is
                # recorded for the next pass.
                failed_info["failure_reason"] = str(driver_error)
                results.append(
                    self._late_retry_failure_result(failed_info, driver_error)
                )
                continue

            try:
                driver.initialize(device_def["config"])
            except MemoryError:
                raise
            # OSError (hardware/I2C) only: a deterministic contract defect
            # escapes to the recovery boundary instead of being re-probed
            # on every pass. Construction errors (e.g. the I2C bus same-bus
            # conflict) escape from _create_driver above, never recorded.
            except OSError as err:
                failed_info["initialization_attempts_used"] += 1
                failed_info["failure_reason"] = str(err)
                results.append(
                    self._late_retry_failure_result(failed_info, err)
                )
                continue

            managed_device = ManagedDevice(
                device_id=device_id,
                device_type=device_def["device_type"],
                driver=driver,
                name=device_def.get("name"),
            )
            managed_device.initialization_attempts_used = (
                failed_info["initialization_attempts_used"] + 1
            )
            self._active_devices.append(managed_device)
            del self._failed_devices[device_id]

            results.append({
                "status": DEVICE_RESULT_BOOT_RECOVERED,
                "device_id": device_id,
                "device": device_def["device_type"],
                "initialization_attempts_used":
                    managed_device.initialization_attempts_used,
            })

        return results

    def _late_retry_failure_result(self, failed_info, error):
        """Result for a failed steady-state retry; the first failure for a
        device is flagged for a warning, repeats are suppressed until the
        device recovers (the record is cleared on success, so a later
        independent failure warns again) -- the reinit-suppression pattern."""
        log_failure_warning = not failed_info.get("retry_failure_logged", False)
        if log_failure_warning:
            failed_info["retry_failure_logged"] = True

        return {
            "status": DEVICE_RESULT_BOOT_RECOVERY_FAILED,
            "device_id": failed_info["id"],
            "device": failed_info["device"],
            "error": str(error),
            "initialization_attempts_used":
                failed_info["initialization_attempts_used"],
            "log_failure_warning": log_failure_warning,
        }

    def get_device_counts(self):
        """Counts only (configured / active / initialization_failed) without
        the per-device snapshot walk: the health message needs the two counts
        and discards everything else the full walk allocates."""
        # "active" means currently READY: a reinitialize_pending device stays
        # in _active_devices (eligible for reinit) but is not counted.
        return {
            "configured": len(self._devices_config),
            "active": sum(
                1
                for managed_device in self._active_devices
                if managed_device.state == DEVICE_STATE_READY
            ),
            "initialization_failed": sum(
                1 for d in self._failed_devices.values()
                if d["state"] == DEVICE_STATE_INITIALIZATION_FAILED
            ),
        }

    def get_status_snapshot(self, now_ms=None):
        """JSON-safe status snapshot of all devices (active + failed), in
        configuration order. now_ms (optional) enables the age fields; the age
        reference is resolved on the shared boot-relative uptime base."""
        # now_ms is only the "include age fields" gate; the reference itself
        # comes from the shared uptime base so ages are correct for any duration.
        now = self._now_ms() if now_ms is not None else None

        device_snapshots = {}

        for managed_device in self._active_devices:
            device_snapshots[managed_device.device_id] = managed_device.get_status_snapshot(now_ms=now)

        for device_id, device_info in self._failed_devices.items():
            snapshot = {
                "id": device_id,
                "device": device_info["device"],
                "state": device_info["state"],
                "initialization_attempts_used": device_info["initialization_attempts_used"],
                "consecutive_read_failures": device_info.get("consecutive_read_failures", 0),
                "total_read_failures": device_info.get("total_read_failures", 0),
                "read_count": device_info.get("read_count", 0),
                "successful_read_count": device_info.get("successful_read_count", 0),
            }

            if "failure_reason" in device_info:
                snapshot["failure_reason"] = device_info["failure_reason"]
            # Failed devices carry no read timestamps, so ages are always None
            snapshot["last_read_age_ms"] = None
            snapshot["last_successful_read_age_ms"] = None

            device_snapshots[device_id] = snapshot

        # Emit in configuration order
        device_status = []
        for device_def in self._devices_config:
            device_id = device_def["id"]
            if device_id in device_snapshots:
                device_status.append(device_snapshots[device_id])

        return {
            "devices": self.get_device_counts(),
            "device_status": device_status,
        }
