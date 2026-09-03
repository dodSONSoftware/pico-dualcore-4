# main.py - Rebuilt dual-core firmware entry point
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import gc
import machine
import os
import time

from hardware import detect_hardware, HARDWARE_TYPE_PICO_W, HARDWARE_TYPE_PICO_2_W
from led_manager import LEDManager


def _runtime_id():
    try:
        uid = machine.unique_id()
        uid_hex = "".join("{:02x}".format(value) for value in uid)
    except MemoryError:
        raise
    except Exception:
        uid_hex = "unknown"

    try:
        nonce = os.urandom(4)
        nonce_hex = "".join("{:02x}".format(value) for value in nonce)
    except MemoryError:
        raise
    except Exception:
        nonce_hex = "{:08x}".format(time.ticks_ms() & 0xFFFFFFFF)

    return "runtime_{}{}".format(uid_hex, nonce_hex)


def main():
    boot_ticks_ms = time.ticks_ms()
    gc.collect()

    # Detect hardware early to validate the board and select the heap
    # thresholds (preferred reserve and hard survival floor).
    hardware = detect_hardware()
    print("[INFO] Hardware detected: {} (heap reserve: {} preferred / {} minimum bytes)".format(
        hardware["hardware_type"],
        hardware["preferred_free_heap_bytes"],
        hardware["minimum_free_heap_bytes"],
    ))

    # Start the boot/connection indication before loading the rest of the firmware.
    led_manager = LEDManager()
    led_manager.set_connecting(True)

    from config import load_wifi_config, split_config
    from config_manager import ConfigManager
    from intercore import InterCore
    from version import FIRMWARE_VERSION

    print("[INFO] Rebuilt dual-core firmware {}".format(FIRMWARE_VERSION))

    # The configuration manager (Core 0-owned) settles the committed
    # configuration -- recovering it from transaction artifacts if a write
    # was interrupted -- before anything else runs.
    config_manager = ConfigManager("config.json")
    config = config_manager.recover()
    wifi_config = load_wifi_config("config-secrets.json")
    core0_config, core1_config = split_config(config)
    config = None

    # The bus is heap-governed: its admission thresholds are the board-specific
    # preferred free-heap reserve (start of pressure handling) and the hard
    # survival floor (hardware.py is the single source of truth), shared by
    # both queues through one heap-admission lock.
    intercore = InterCore(
        minimum_free_heap_bytes=hardware["minimum_free_heap_bytes"],
        preferred_free_heap_bytes=hardware["preferred_free_heap_bytes"],
    )

    from core0 import Core0

    # One runtime ID for this boot; both cores must agree on it.
    runtime_id = _runtime_id()

    core0 = Core0(
        intercore,
        core0_config,
        wifi_config,
        runtime_id,
        boot_ticks_ms,
        led_manager,
        config_manager,
    )
    core0_config = None
    wifi_config = None

    # Recovery boundary for the operational phase. Everything above is
    # deterministic startup validation (hardware, config, construction) and
    # intentionally fails fast: a misconfigured or unsupported board must
    # stay down with a diagnosable error, not reboot forever. From here on
    # the firmware is in operational runtime, and the supervision is still
    # one-directional: Core 0's heartbeat watchdog recovers a dead Core 1,
    # but nothing supervises Core 0 itself. A Core 0 that terminates (a
    # MemoryError, an unexpected exception escaping start()/run()) would
    # leave the board with no networking, no Core 1 supervision, and no
    # recovery — a transient failure turned into a permanent outage until
    # something external resets the Pico. An unrecoverable Core 0 exception
    # is therefore a controlled board reset, not application termination.
    try:
        # Core 0 establishes the network before any Core 1 module is imported.
        core0.start()

        gc.collect()
        import _thread
        from core1 import core1_main

        _thread.start_new_thread(core1_main, (intercore, core1_config, boot_ticks_ms, runtime_id))
        core1_config = None
        print("[INFO] Core 1 started after Wi-Fi + MQTT")

        core0.run()
    except MemoryError:
        # The heap is exhausted: this handler must not allocate, or the
        # reset — the whole point of the boundary — could never run.
        # machine.reset() is allocation-free and never returns on hardware.
        machine.reset()
    except Exception as err:
        # Not a MemoryError, so the heap is serviceable and one diagnostic
        # line before the reset is safe.
        print("[FATAL] Core 0 unrecoverable exception in operational runtime: {} - resetting".format(err))
        machine.reset()


if __name__ == "__main__":
    main()
