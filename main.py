# main.py - Rebuilt dual-core firmware entry point
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import gc
import machine
import os
import time

from hardware import (
    detect_hardware,
    derive_boot_reason,
    read_last_reset_cause,
    HARDWARE_TYPE_PICO_W,
    HARDWARE_TYPE_PICO_2_W,
)
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
    # Boundary 1 (boot): capture the post-boot-cleanup free heap as the first
    # real memory checkpoint. It seeds the shared MemoryStats low-watermark and
    # is the reference the board reserve is defended against for this runtime.
    boot_free_heap = gc.mem_free()

    # Detect hardware early to validate the board and select heap reserve.
    hardware = detect_hardware()
    # The reset cause describes how this boot began: read it exactly once,
    # here, and preserve it for the lifetime of the runtime. It is historical
    # diagnostic information and never affects health classification.
    hardware["last_reset_cause"] = read_last_reset_cause()
    # Boot reason is derived once from that single read (semantic face of the
    # reset cause; the mapping lives in hardware.derive_boot_reason).
    hardware["boot_reason"] = derive_boot_reason(hardware["last_reset_cause"])
    print("[INFO] Hardware detected: {} (heap reserve: {} bytes)".format(
        hardware["hardware_type"],
        hardware["minimum_free_heap_bytes"],
    ))

    # Start the boot/connection indication before loading the rest of the firmware.
    led_manager = LEDManager()
    led_manager.set_connecting(True)

    from config import (
        ConfigState,
        load_wifi_config,
        recover_config_file,
        split_config,
    )
    from intercore import InterCore
    from version import FIRMWARE_BUILD_COMMIT, FIRMWARE_VERSION

    print("[INFO] Rebuilt dual-core firmware {} (build: {})".format(
        FIRMWARE_VERSION, FIRMWARE_BUILD_COMMIT))

    # Boot-time configuration recovery: a valid primary wins; a valid backup
    # is promoted over an invalid primary (and reported); if neither is valid
    # this raises and startup fails clearly -- no default is invented. The
    # checksum is over the exact committed bytes (config-secrets.json is
    # separate and never part of it).
    config, config_checksum, config_recovery = recover_config_file("config.json")
    wifi_config = load_wifi_config("config-secrets.json")
    core0_config, core1_config, bus_config = split_config(config)

    # The outbound queue's entry ceiling is the fixed 64-entry sanity guard
    # (intercore.MAX_OUTBOUND_QUEUE_ENTRIES), not a user knob. Admission is
    # governed by the board's free-heap reserve, which the shared MemoryStats
    # (seeded from the boot checkpoint) defends.
    intercore = InterCore(
        minimum_free_heap_bytes=hardware["minimum_free_heap_bytes"],
        initial_free_heap_bytes=boot_free_heap,
        event_max=bus_config["max_intercore_event_entries"],
    )
    # The committed-configuration view, shared by both cores (Core 0 owns the
    # transaction coordinator; Core 1 never names it). Holds the committed
    # snapshot, generation, checksum, and reboot_required bookkeeping.
    intercore.config_state = ConfigState(config, config_checksum)
    bus_config = None
    config = None

    # Publish the startup hardware snapshot (board identity, heap reserve,
    # last reset cause) before any Core 0/Core 1 operation begins. It is
    # immutable for this runtime and is the single source for the hardware
    # identity fields in health and system-information reports.
    intercore.state_mailboxes.set_hardware(hardware)

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
    )
    core0_config = None
    wifi_config = None

    # Core 0 establishes the network before any Core 1 module is imported.
    # When a valid backup was promoted at boot, core0 surfaces the
    # configuration_recovered log once the network is up.
    core0.start(config_recovery_event=config_recovery)

    # Boundary 2 (before Core 1 starts): a tracked, authoritative collect. It
    # runs while no Core 1 thread is live, updates the low-watermark minimum
    # from the pre/post values, and records the collect statistics.
    intercore.memory_stats.collect()
    import _thread
    from core1 import core1_main

    _thread.start_new_thread(core1_main, (intercore, core1_config, boot_ticks_ms, runtime_id))
    core1_config = None
    print("[INFO] Core 1 started after Wi-Fi + MQTT")

    core0.run()


if __name__ == "__main__":
    main()
