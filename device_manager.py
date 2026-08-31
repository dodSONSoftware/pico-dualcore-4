# device_manager.py - Device lifecycle management
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import time
from device_factory import create_device
from message_protocol import is_json_safe

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
        """Mark this device for reinitialization on the next cycle."""
        self.reinitialize_pending = True
        self.state = DEVICE_STATE_REINITIALIZE_PENDING

    def clear_reinitialize_pending(self):
        """Clear reinitialize pending state after successful reinit."""
        self.reinitialize_pending = False
        self.state = DEVICE_STATE_READY
        self.consecutive_read_failures = 0
        self.reinit_failure_logged = False  # Reset suppression flag on success

    def set_reinit_failure_logged(self):
        """Mark that a reinitialization failure has been logged (for suppression)."""
        self.reinit_failure_logged = True

    def should_suppress_reinit_failure(self):
        """Check if reinitialization failure logging should be suppressed."""
        return self.reinit_failure_logged

    def record_read_failure(self):
        """Record a read failure for this device."""
        self.consecutive_read_failures += 1
        self.total_read_failures += 1

    def record_read_success(self):
        """Record a successful read, resetting consecutive failures."""
        self.consecutive_read_failures = 0

    def get_status_snapshot(self, now_ms=None):
        """
        Get a JSON-safe status snapshot of this device.

        Args:
            now_ms: Optional monotonic timestamp in milliseconds for age calculations

        Returns:
            dict: Device status for telemetry reporting
        """
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

        # Add age fields if now_ms is provided
        if now_ms is not None:
            snapshot["last_read_age_ms"] = None
            snapshot["last_successful_read_age_ms"] = None
            if self.last_read_ms is not None:
                snapshot["last_read_age_ms"] = time.ticks_diff(now_ms, self.last_read_ms)
            if self.last_successful_read_ms is not None:
                snapshot["last_successful_read_age_ms"] = time.ticks_diff(now_ms, self.last_successful_read_ms)

        return snapshot


class DeviceManager:
    """Manages device lifecycle for Core 1."""

    def __init__(self, config, system_information=None, activity_refresh=None):
        self._active_devices = []
        self._failed_devices = {}

        # Device configuration
        self._device_initialization_attempts = config["device_initialization_attempts"]
        self._device_initialization_retry_delay_ms = config["device_initialization_retry_delay_ms"]
        self._device_read_failure_threshold = config["device_read_failure_threshold"]
        self._devices_config = config["devices"]

        self._system_information = system_information

        # Optional liveness-stamp refresh callback, owned by Core 1 (which
        # constructs this manager) and invoked at initialization progress
        # boundaries. Without it, a legitimately long initialization
        # (several devices x attempts x retry delays) would age Core 1's
        # liveness stamp past Core 0's watchdog bound and reset a healthy
        # board. A wedge inside a driver call stops the refresh and is still
        # caught. None (the default) leaves behavior unchanged.
        self._activity_refresh = activity_refresh

    def _refresh_activity(self):
        """Refresh Core 1's liveness stamp at a progress boundary, if armed."""
        if self._activity_refresh is not None:
            self._activity_refresh()

    def _initialize_single_device(self, device_def):
        """Initialize a single device from config."""
        device_id = device_def["id"]
        device_type = device_def["device_type"]
        sensor_type = device_def.get("sensor_type", "unknown")
        name = device_def.get("name")
        attempt_logs = []

        # Step 1: Construct the driver
        driver, driver_error = self._create_driver(device_def)
        if driver is None:
            return self._record_driver_failure(device_id, device_type, driver_error)

        # Step 2: Initialize the driver with retries
        attempts_used, initialized = self._initialize_driver_with_retries(
            driver, device_def, device_id, device_type, attempt_logs
        )

        if not initialized:
            return self._record_initialization_failure(
                device_id, device_type, attempts_used, attempt_logs
            )

        # Step 3: Success - create ManagedDevice
        return self._record_success(
            device_id, device_type, sensor_type, driver, attempts_used, attempt_logs, name
        )

    def _create_driver(self, device_def):
        """Create device driver from config."""
        try:
            driver = create_device(device_def, self._system_information)
            return driver, None
        except MemoryError:
            raise
        except Exception as err:
            return None, err

    def _initialize_driver_with_retries(self, driver, device_def, device_id, device_type, attempt_logs):
        """Initialize driver with retry logic."""
        attempts_used = 0
        last_error = None
        initialized = False
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
                attempt_logs.append({
                    "device_id": device_id,
                    "device_type": device_type,
                    "attempt": attempt,
                    "success": True,
                    "error": None,
                })
                initialized = True
                break
            except MemoryError:
                raise
            except Exception as err:
                last_error = str(err)
                attempt_logs.append({
                    "device_id": device_id,
                    "device_type": device_type,
                    "attempt": attempt,
                    "success": False,
                    "error": last_error,
                })
                if attempt < self._device_initialization_attempts:
                    # Not the final attempt - wait before retry
                    time.sleep_ms(self._device_initialization_retry_delay_ms)

        return attempts_used, initialized

    def _record_driver_failure(self, device_id, device_type, error):
        """Record driver construction failure."""
        attempt_logs = [{
            "device_id": device_id,
            "device_type": device_type,
            "attempt": 1,
            "success": False,
            "error": str(error),
        }]
        failed_details = [{
            "device_id": device_id,
            "device_type": device_type,
            "initialization_attempts_used": 1,
            "last_error": str(error),
        }]
        self._failed_devices[device_id] = {
            "id": device_id,
            "device": device_type,
            "state": DEVICE_STATE_INITIALIZATION_FAILED,
            "initialization_attempts_used": 1,
            "failure_reason": str(error),
        }
        return {
            "success": False,
            "device_id": device_id,
            "device_type": device_type,
            "attempt_logs": attempt_logs,
            "failed_details": failed_details,
        }

    def _record_initialization_failure(self, device_id, device_type, attempts_used, attempt_logs):
        """Record driver initialization failure."""
        last_error = attempt_logs[-1]["error"] if attempt_logs else "Unknown error"

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
            "attempt_logs": attempt_logs,
            "failed_details": failed_details,
        }

    def _record_success(self, device_id, device_type, sensor_type, driver, attempts_used, attempt_logs, name=None):
        """Record successful device initialization."""
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
            "attempt_logs": attempt_logs,
            "failed_details": [],
        }

    def initialize_devices(self):
        """
        Initialize all configured devices.

        For every configured device, in configuration order:
        1. Construct the driver through DeviceFactory (once per device)
        2. Attempt initialization up to device_initialization_attempts
        3. Wait device_initialization_retry_delay_ms between failed attempts
        4. On success: add to active devices
        5. On final failure: record as failed, continue to next device

        This method must be called on Core 1.

        Returns:
            tuple: (initialized_count, failed_device_details, attempt_logs)
                   where failed_device_details is a list of dicts with:
                       - device_id: str
                       - device_type: str
                       - initialization_attempts_used: int
                   and attempt_logs is a list of dicts with:
                       - device_id: str
                       - device_type: str
                       - attempt: int (1-indexed)
                       - success: bool
                       - error: str or None
        """
        initialized_count = 0
        all_failed_device_details = []
        all_attempt_logs = []

        for device_def in self._devices_config:
            # Progress boundary: covers driver construction, which precedes
            # the attempt loop below and could itself take long.
            self._refresh_activity()
            result = self._initialize_single_device(device_def)
            all_attempt_logs.extend(result["attempt_logs"])
            all_failed_device_details.extend(result["failed_details"])
            if result["success"]:
                initialized_count += 1

        return initialized_count, all_failed_device_details, all_attempt_logs

    def get_active_devices(self):
        """
        Get the active managed devices in configuration order.

        Returns the internal list itself, not a copy: Core 1 exclusively
        owns device membership (only this manager mutates it, at startup),
        membership is fixed after initialize_devices() -- reinitialization
        never adds or removes entries -- and the caller iterates it
        read-only. A per-cycle copy would be needless heap churn on the
        hot telemetry path.

        Returns:
            list: ManagedDevice instances (the live membership list; do not mutate)
        """
        return self._active_devices

    def process_device(self, managed_device):
        """
        Process one device for the current cycle.

        Handles:
        - Normal read (if reinitialize_pending is False)
        - Reinitialization (if reinitialize_pending is True)
        - Failure tracking

        Args:
            managed_device: ManagedDevice instance to process

        Returns:
            dict: Result with status and metadata
        """
        if managed_device.reinitialize_pending:
            return self._process_reinitialization(managed_device)

        return self._process_normal_read(managed_device)

    def _process_normal_read(self, managed_device):
        """
        Process a normal read for a device.

        Args:
            managed_device: ManagedDevice instance

        Returns:
            dict: Result with telemetry or failure info
        """
        try:
            # Record read attempt
            managed_device.read_count += 1
            managed_device.last_read_ms = time.ticks_ms()

            telemetry = managed_device.driver.read()

            # Validate telemetry before recording success
            # A valid telemetry must be a dictionary with at least one key
            if not isinstance(telemetry, dict) or len(telemetry) == 0:
                raise TypeError("Driver read() must return a non-empty dictionary")

            # Use the existing JSON safety validator to ensure telemetry
            # can be safely serialized for message bus
            if not is_json_safe(telemetry):
                raise TypeError("Driver read() returned non-JSON-safe data")

            # Record successful read
            managed_device.successful_read_count += 1
            managed_device.last_successful_read_ms = time.ticks_ms()

            previous_failures = managed_device.consecutive_read_failures
            managed_device.record_read_success()

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

    def _process_reinitialization(self, managed_device):
        """
        Process reinitialization for a device.

        Reuses the configured initialization retry policy to handle
        transient failures during runtime recovery. A single failed
        attempt does not permanently remove the device.

        Args:
            managed_device: ManagedDevice instance

        Returns:
            dict: Result with reinitialization status
        """
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
                    # Progress boundary before the retry sleep: the sleep
                    # itself must not age the liveness stamp either.
                    self._refresh_activity()
                    time.sleep_ms(self._device_initialization_retry_delay_ms)

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
        """
        Get a JSON-safe status snapshot of all devices (active + failed).

        Args:
            now_ms: Optional monotonic timestamp in milliseconds for age calculations

        Failed devices are included in the status array with their final state
        and diagnostic information. The order is deterministic: devices appear
        in configuration order.

        Returns:
            dict: Device status for telemetry reporting
        """
        # Build a map of device_id -> status snapshot for all devices
        device_snapshots = {}

        # Add active devices in order (preserved by list order)
        for managed_device in self._active_devices:
            device_snapshots[managed_device.device_id] = managed_device.get_status_snapshot(now_ms=now_ms)

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
