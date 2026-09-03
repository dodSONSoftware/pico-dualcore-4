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


class ManagedDevice:
    """Runtime wrapper around a successfully initialized device."""

    def __init__(self, device_id, device_type, sensor_type, driver, name=None):
        self.device_id = device_id
        self.device_type = device_type
        self.sensor_type = sensor_type
        self.driver = driver
        self.name = name

        # Runtime state
        self.state = DEVICE_STATE_READY

        # Startup initialization tracking
        self.initialization_attempts_used = 0

        # Runtime failure tracking
        self.consecutive_read_failures = 0
        self.total_read_failures = 0

        # Read tracking (counters)
        self.read_count = 0
        self.successful_read_count = 0

        # Read timing (timestamps)
        self.last_read_ms = None
        self.last_successful_read_ms = None

        # Reinitialization state
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

        if self.state == DEVICE_STATE_READY:
            snapshot["sensor_type"] = self.sensor_type

        # Add optional fields if present
        if self.name is not None:
            snapshot["name"] = self.name

        # Add age fields if now_ms is provided. now_ms and the stored read
        # timestamps both sit on the shared boot-relative uptime base (set by
        # DeviceManager._now_ms()), so the age is a plain subtraction, correct
        # for any duration -- where a one-shot ticks_diff(now, last_read) wraps
        # once a read is more than half a tick period old.
        if now_ms is not None:
            snapshot["last_read_age_ms"] = None
            snapshot["last_successful_read_age_ms"] = None
            if self.last_read_ms is not None:
                snapshot["last_read_age_ms"] = now_ms - self.last_read_ms
            if self.last_successful_read_ms is not None:
                snapshot["last_successful_read_age_ms"] = now_ms - self.last_successful_read_ms

        return snapshot


class DeviceManager:
    """Manages device lifecycle for Core 1."""

    def __init__(self, config, system_information=None, activity_refresh=None, uptime_state=None):
        self._active_devices = []
        self._failed_devices = {}

        # Device configuration
        self._device_initialization_attempts = config["device_initialization_attempts"]
        self._device_initialization_retry_delay_ms = config["device_initialization_retry_delay_ms"]
        self._device_read_failure_threshold = config["device_read_failure_threshold"]
        self._devices_config = config["devices"]

        self._system_information = system_information

        # Core 1's accumulated-uptime state (same object Core 1 advances), the
        # single source of truth for the read-age fields. When None (host tests
        # that do not wire it), the manager falls back to raw ticks.
        self._uptime_state = uptime_state

        # Optional liveness-stamp refresh callback, owned by Core 1 (which
        # constructs this manager) and invoked at progress boundaries:
        # initialization (per device, per attempt, per retry-sleep step) and
        # the normal telemetry pass (per device, via process_device()).
        # Without it, a legitimately long initialization (several devices x
        # attempts x retry delays) or a telemetry pass whose cumulative reads
        # exceed the watchdog bound (each read under it) would age Core 1's
        # liveness stamp past Core 0's watchdog bound and reset a healthy
        # board. A wedge inside a driver call stops the refresh and is still
        # caught. None (the default) leaves behavior unchanged.
        self._activity_refresh = activity_refresh

    def _refresh_activity(self):
        """Refresh Core 1's liveness stamp at a progress boundary, if armed."""
        if self._activity_refresh is not None:
            self._activity_refresh()

    def _now_ms(self):
        """Current time on the shared boot-relative uptime base.

        Accumulated uptime (the same single source of truth Core 1 uses for its
        UTC timestamp and uptime fields) when a Core 1 uptime state is wired; raw
        ticks otherwise (host tests without a wired state). Both the stored read
        timestamps and every read-age subtraction use this base, so a device that
        stays failed/reinitializing past half a tick period still reports a
        correct read age -- where a one-shot ticks_diff against the read tick
        would wrap."""
        if self._uptime_state is not None:
            return current_uptime_ms(self._uptime_state)
        return time.ticks_ms()

    # One liveness-refresh step inside a long retry sleep.
    _ACTIVITY_REFRESH_STEP_MS = 100

    def _sleep_with_activity_refresh(self, delay_ms):
        """Sleep in small steps, refreshing the liveness stamp at each step boundary.

        ``device_initialization_retry_delay_ms`` is a non-negative config value with no upper bound; a monolithic sleep would age the stamp past Core 0's watchdog bound for a sufficiently long configured delay. Stepping the sleep keeps the stamp fresh for the whole delay while a driver wedged inside a call still stops the refreshes and is caught."""
        while delay_ms > 0:
            step_ms = min(delay_ms, self._ACTIVITY_REFRESH_STEP_MS)
            time.sleep_ms(step_ms)
            self._refresh_activity()
            delay_ms -= step_ms

    def _initialize_single_device(self, device_def):
        device_id = device_def["id"]
        device_type = device_def["device_type"]
        sensor_type = device_def.get("sensor_type", "unknown")
        name = device_def.get("name")

        # Step 1: Construct the driver
        driver, driver_error = self._create_driver(device_def)
        if driver is None:
            return self._record_driver_failure(device_id, device_type, driver_error)

        # Step 2: Initialize the driver with retries
        attempts_used, initialized, last_error = self._initialize_driver_with_retries(
            driver, device_def
        )

        if not initialized:
            return self._record_initialization_failure(
                device_id, device_type, attempts_used, last_error
            )

        # Step 3: Success - create ManagedDevice
        return self._record_success(
            device_id, device_type, sensor_type, driver, attempts_used, name=name
        )

    def _create_driver(self, device_def):
        try:
            driver = create_device(device_def, self._system_information)
            return driver, None
        except MemoryError:
            raise
        except Exception as err:
            return None, err

    def _initialize_driver_with_retries(self, driver, device_def):
        # Only the attempt count and the final error are retained: the recovery
        # algorithm needs neither per-attempt history, and retaining it grew
        # heap on the startup-failure path for a large configured retry count.
        attempts_used = 0
        last_error = None
        device_config = device_def["config"]

        for attempt in range(1, self._device_initialization_attempts + 1):
            attempts_used = attempt
            # Progress boundary: covers the retry delay slept before this
            # attempt, so a legitimate retry sequence does not age the
            # liveness stamp past Core 0's watchdog bound.
            self._refresh_activity()
            try:
                # Pass config to initialize - this is the new interface
                driver.initialize(device_config)
                # Initialization succeeded (returns None, raises on failure)
                return attempts_used, True, None
            except MemoryError:
                raise
            except Exception as err:
                last_error = str(err)
                if attempt < self._device_initialization_attempts:
                    # Not the final attempt - wait before retry; the sleep
                    # refreshes the liveness stamp at each step boundary, so
                    # a long configured delay does not age the stamp past
                    # Core 0's watchdog bound.
                    self._sleep_with_activity_refresh(self._device_initialization_retry_delay_ms)

        return attempts_used, False, last_error

    def _record_driver_failure(self, device_id, device_type, error):
        last_error = str(error)
        failed_details = [{
            "device_id": device_id,
            "device_type": device_type,
            "initialization_attempts_used": 1,
            "last_error": last_error,
        }]
        self._failed_devices[device_id] = {
            "id": device_id,
            "device": device_type,
            "state": DEVICE_STATE_INITIALIZATION_FAILED,
            "initialization_attempts_used": 1,
            "failure_reason": last_error,
        }
        return {
            "success": False,
            "device_id": device_id,
            "device_type": device_type,
            "failed_details": failed_details,
        }

    def _record_initialization_failure(self, device_id, device_type, attempts_used, last_error):
        failed_details = [{
            "device_id": device_id,
            "device_type": device_type,
            "initialization_attempts_used": attempts_used,
            "last_error": last_error,
        }]
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
            "failed_details": failed_details,
        }

    def _record_success(self, device_id, device_type, sensor_type, driver, attempts_used, name=None):
        managed_device = ManagedDevice(
            device_id=device_id,
            device_type=device_type,
            sensor_type=sensor_type,
            driver=driver,
            name=name,
        )
        managed_device.initialization_attempts_used = attempts_used

        self._active_devices.append(managed_device)

        return {
            "success": True,
            "device_id": device_id,
            "device_type": device_type,
            "failed_details": [],
        }

    def initialize_devices(self):
        """Initialize all configured devices, in configuration order.

        Construct the driver once per device, then attempt initialization up to device_initialization_attempts (retry delay between attempts); on final failure record the device and continue. Must be called on Core 1. Returns (initialized_count, failed_device_details), where failed_device_details carries each failed device's attempts_used and last_error (no per-attempt history)."""
        initialized_count = 0
        all_failed_device_details = []

        for device_def in self._devices_config:
            # Progress boundary: covers driver construction, which precedes
            # the attempt loop below and could itself take long.
            self._refresh_activity()
            result = self._initialize_single_device(device_def)
            all_failed_device_details.extend(result["failed_details"])
            if result["success"]:
                initialized_count += 1

        return initialized_count, all_failed_device_details

    def get_active_devices(self):
        """Get the active managed devices in configuration order.

        Returns the internal list itself, not a copy: membership is fixed after initialize_devices() and a per-cycle copy would be needless heap churn on the hot telemetry path (do not mutate)."""
        return self._active_devices

    def process_device(self, managed_device):
        """Process one device for the current cycle (normal read, or reinitialization if pending).

        Refreshes the liveness stamp at each device boundary: the telemetry pass refreshes once per device, not once per pass, so the watchdog measures the duration of one device operation (a wedge) rather than the cumulative duration of all of them (several legitimately slow reads that individually stay under the bound)."""
        self._refresh_activity()
        if managed_device.reinitialize_pending:
            return self._process_reinitialization(managed_device)

        return self._process_normal_read(managed_device)

    def _process_normal_read(self, managed_device):
        """Process a normal read for a device; validate the telemetry and record the outcome."""
        try:
            # Record read attempt. The timestamp is boot-relative accumulated
            # uptime (not a raw tick) so the read-age stays correct for any
            # duration, including a device stuck in failure/reinit past half a
            # tick period.
            managed_device.read_count += 1
            managed_device.last_read_ms = self._now_ms()

            telemetry = managed_device.driver.read()

            # Validate telemetry before recording success
            # A valid telemetry must be a dictionary with at least one key
            if not isinstance(telemetry, dict) or len(telemetry) == 0:
                raise TypeError("Driver read() must return a non-empty dictionary")

            # Use the existing JSON safety validator to ensure telemetry
            # can be safely serialized for message bus
            if not is_json_safe(telemetry):
                raise TypeError("Driver read() returned non-JSON-safe data")

            # Record successful read (same boot-relative uptime base as
            # last_read_ms, so the age is a plain subtraction on that base).
            managed_device.successful_read_count += 1
            managed_device.last_successful_read_ms = self._now_ms()

            previous_failures = managed_device.consecutive_read_failures
            managed_device.record_read_success()

            self._refresh_system_information_self_status(managed_device, telemetry)

            return {
                "status": DEVICE_RESULT_TELEMETRY,
                "device_id": managed_device.device_id,
                "device": managed_device.device_type,
                "sensor_type": managed_device.sensor_type,
                "name": managed_device.name,
                "telemetry": telemetry,
                "recovered": previous_failures > 0,
                "previous_consecutive_read_failures": previous_failures,
                "remove": False,
            }
        except MemoryError:
            raise
        except Exception as err:
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
                "remove": False,
            }

    def _refresh_system_information_self_status(self, managed_device, telemetry):
        """Refresh this system-information device's own status after read success.

        A system-information read that includes the ``device_status`` section captures the manager's snapshot while this device's own success is not yet committed (read_count has been incremented, successful_read_count has not), so the telemetry would otherwise carry a one-read-stale self-entry. After the success is committed, replace only this device's own entry with a fresh snapshot; every other device's entry keeps exactly what the original read captured. A no-op for other device types and when ``device_status`` was not in the read's payload (the configured include list stays authoritative)."""
        if managed_device.device_type != "system-information":
            return

        device_status = telemetry.get("device_status")
        if not isinstance(device_status, list):
            return

        # Same boot-relative uptime base as the stored read timestamps, so the
        # age is a plain subtraction on that base (correct for any duration).
        now_ms = self._now_ms()
        fresh_status = managed_device.get_status_snapshot(now_ms=now_ms)

        for index, status in enumerate(device_status):
            if isinstance(status, dict) and status.get("id") == managed_device.device_id:
                device_status[index] = fresh_status
                return

    def _process_reinitialization(self, managed_device):
        """Process reinitialization for a device, reusing the configured initialization retry policy.

        A single failed attempt does not remove the device: it stays reinitialize_pending for the next cycle."""
        # Resolve the device definition once, before the retry loop
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
            # Progress boundary, the same strategy as the startup
            # initialization path: a legitimate runtime reinitialization
            # (attempts plus retry delays) must not age Core 1's liveness
            # stamp past Core 0's watchdog bound, while a wedge inside
            # driver.initialize() stops the refreshes and is still caught.
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
                    "remove": False,
                }
            except MemoryError:
                raise
            except Exception as err:
                last_error = str(err)
                # If not the final attempt, wait before retry
                if attempt < self._device_initialization_attempts:
                    # Wait before retry; the sleep refreshes the liveness
                    # stamp at each step boundary, so a long configured
                    # delay does not age the stamp past Core 0's watchdog
                    # bound (the stamp was current before the attempt).
                    self._sleep_with_activity_refresh(self._device_initialization_retry_delay_ms)

        # All reinitialization attempts exhausted
        # The device remains in reinitialize_pending state for retry on next cycle
        # Do NOT mark for removal - transient failures should be retried
        #
        # A device that keeps failing reinit warns on every cycle. Log the first
        # failure and suppress the repeats: the flag lives on the ManagedDevice
        # and is cleared by a successful reinit (clear_reinitialize_pending), so
        # an independent later failure logs again.
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
            "remove": False,
        }

    def get_status_snapshot(self, now_ms=None):
        """Get a JSON-safe status snapshot of all devices (active + failed), in deterministic configuration order.

        Failed devices carry their final state and diagnostic info; now_ms (optional) enables the age fields. When the age fields are enabled the actual reference is resolved from the shared boot-relative uptime base (not the raw now_ms passed in), so the ages stay correct for any duration."""
        # Resolve the age reference on the shared boot-relative uptime base when
        # age fields are requested (None otherwise): now_ms is kept only as the
        # "include age fields" gate, while the timestamps and the subtraction
        # share one base, so the age is correct for any duration.
        now = self._now_ms() if now_ms is not None else None

        # Build a map of device_id -> status snapshot for all devices
        device_snapshots = {}

        # Add active devices in order (preserved by list order)
        for managed_device in self._active_devices:
            device_snapshots[managed_device.device_id] = managed_device.get_status_snapshot(now_ms=now)

        # Add failed devices with their diagnostic info
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

            # Add optional fields if present
            if "failure_reason" in device_info:
                snapshot["failure_reason"] = device_info["failure_reason"]
            # Failed devices carry no read timestamps, so ages are always None
            snapshot["last_read_age_ms"] = None
            snapshot["last_successful_read_age_ms"] = None

            device_snapshots[device_id] = snapshot

        # Return snapshots in configuration order
        device_status = []
        for device_def in self._devices_config:
            device_id = device_def["id"]
            if device_id in device_snapshots:
                device_status.append(device_snapshots[device_id])

        # "active" means currently READY. A device in reinitialize_pending state
        # stays in _active_devices (it must remain eligible for reinitialization),
        # but it is not actively producing telemetry, so it is not counted.
        active_count = sum(
            1
            for managed_device in self._active_devices
            if managed_device.state == DEVICE_STATE_READY
        )

        # Count devices in each state
        initialization_failed = sum(
            1 for d in self._failed_devices.values()
            if d["state"] == DEVICE_STATE_INITIALIZATION_FAILED
        )

        return {
            "devices": {
                "configured": len(self._devices_config),
                "active": active_count,
                "initialization_failed": initialization_failed,
            },
            "device_status": device_status,
        }
