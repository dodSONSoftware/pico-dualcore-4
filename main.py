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

    # Detect hardware early to validate the board and select heap reserve.
    hardware = detect_hardware()
    print("[INFO] Hardware detected: {} (heap reserve: {} bytes)".format(
        hardware["hardware_type"],
        hardware["minimum_free_heap_bytes"],
    ))

    # Start the boot/connection indication before loading the rest of the firmware.
    led_manager = LEDManager()
    led_manager.set_connecting(True)

    from config import load_config, load_wifi_config, split_config
    from intercore import InterCore
    from version import FIRMWARE_VERSION

    print("[INFO] Rebuilt dual-core firmware {}".format(FIRMWARE_VERSION))

    config = load_config("config.json")
    wifi_config = load_wifi_config("config-secrets.json")
    core0_config, core1_config, bus_config = split_config(config)
    config = None

    intercore = InterCore(
        outbound_max=bus_config["max_outbound_queue_entries"],
        event_max=bus_config["max_intercore_event_entries"],
    )
    bus_config = None

    from core0 import Core0

    core0 = Core0(
        intercore,
        core0_config,
        wifi_config,
        _runtime_id(),
        boot_ticks_ms,
        led_manager,
    )
    core0_config = None
    wifi_config = None

    # Core 0 establishes the network before any Core 1 module is imported.
    core0.start()

    gc.collect()
    import _thread
    from core1 import core1_main

    _thread.start_new_thread(core1_main, (intercore, core1_config, boot_ticks_ms))
    core1_config = None
    print("[INFO] Core 1 started after Wi-Fi + MQTT")

    core0.run()


if __name__ == "__main__":
    main()
