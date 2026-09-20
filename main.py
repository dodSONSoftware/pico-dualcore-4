# main.py - Rebuilt dual-core firmware entry point
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT

import gc
import machine
import os
import time

from hardware import detect_hardware
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

    # Detect hardware first: fail fast on an unsupported board and select the
    # board heap thresholds (preferred reserve and hard survival floor).
    hardware = detect_hardware()
    print("[INFO] Hardware detected: {} (heap reserve: {} preferred / {} minimum bytes)".format(
        hardware["hardware_type"],
        hardware["preferred_free_heap_bytes"],
        hardware["minimum_free_heap_bytes"],
    ))

    led_manager = LEDManager()
    led_manager.set_connecting(True)

    from config import load_wifi_config, split_config
    from config_manager import ConfigManager
    from intercore import InterCore
    from version import FIRMWARE_VERSION

    print("[INFO] Rebuilt dual-core firmware {}".format(FIRMWARE_VERSION))

    # Settle the committed configuration (recovering it from transaction
    # artifacts if a write was interrupted) before anything else runs.
    config_manager = ConfigManager("config.json")
    config = config_manager.recover()
    wifi_config = load_wifi_config("config-secrets.json")
    # Read before split_config discards the full config: the outbound queue's
    # count ceiling, subordinate to the heap policy.
    outbound_queue_max_messages = config["outbound_queue_max_messages"]
    core0_config, core1_config = split_config(config)
    config = None

    intercore = InterCore(
        minimum_free_heap_bytes=hardware["minimum_free_heap_bytes"],
        preferred_free_heap_bytes=hardware["preferred_free_heap_bytes"],
        outbound_queue_max_messages=outbound_queue_max_messages,
    )
    hardware = None

    # One runtime ID for this boot; both cores must agree on it.
    runtime_id = _runtime_id()

    # Spawn Core 1 first -- before the core1/core0 imports and the network
    # bring-up -- so its ~4 KiB stack gets a contiguous GC-pool allocation
    # before the Pico W heap fragments (late spawns MemoryErrored into the
    # silent reset boundary and reboot-looped; see ARCHITECTURE.md).
    import _thread

    def _core1_thread_entry(bus, cfg, boot_ms, rid):
        # Stamp liveness at spawn so a worker death in the wait is bounded by
        # Core 0's stale-heartbeat watchdog instead of going unnoticed.
        bus.state_mailboxes.set_core_1_activity_ms(time.ticks_ms())
        while True:
            snapshot = bus.state_mailboxes.get_network_snapshot()
            if snapshot is not None and snapshot.get("network_stack_ready"):
                break
            time.sleep_ms(100)
            bus.state_mailboxes.set_core_1_activity_ms(time.ticks_ms())
        print("[INFO] Core 1 ready; running core1_main (free heap {} bytes)".format(gc.mem_free()))
        core1_main(bus, cfg, boot_ms, rid)

    _thread.start_new_thread(_core1_thread_entry, (intercore, core1_config, boot_ticks_ms, runtime_id))
    core1_config = None
    print("[INFO] Core 1 worker spawned; starts when the network stack reports ready")

    # Reclaim the startup garbage (the nulled config graph, the recovery parse
    # residue, the thread-spawn residue) and coalesce the free runs before the
    # first heavy import: on the Pico W a fragmented pool can no longer yield
    # the contiguous run the import's code objects need -- 0.4.90 hit this at
    # the core0 import, and the grown core chain now exhausts it at the core1
    # import. Mirrors the reclaim before the core0 import below.
    gc.collect()

    # Import the Core 1 chain here, on the main thread, before the core0
    # import: the parse buffer is a C-heap (non-GC) allocation the network
    # bring-up exhausts, and a late import MemoryErrors where a late spawn
    # cannot (see ARCHITECTURE.md). core1_main still runs on the worker.
    from core1 import core1_main
    print("[INFO] Core 1 modules imported pre-network (free heap {} bytes)".format(gc.mem_free()))

    # Coalesce the pool before the heaviest import: it sits above the
    # recovery boundary below, so a fragmentation MemoryError there (no
    # contiguous run despite tens of KiB free) would reboot-loop the board.
    gc.collect()
    print("[INFO] Heap reclaimed before Core 0 import (free heap {} bytes)".format(gc.mem_free()))

    from core0 import Core0

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

    # Recovery boundary for the operational phase: everything above is
    # deterministic startup validation that fails fast (a misconfigured board
    # must stay down, not reboot forever); from here an unrecoverable Core 0
    # exception is a controlled board reset, not application termination.
    try:
        core0.start()

        core0.run()
    except MemoryError:
        # The heap is exhausted: this handler must not allocate, or the
        # reset could never run (machine.reset() is allocation-free).
        machine.reset()
    except Exception as err:
        # Not a MemoryError, so the heap is serviceable and one diagnostic
        # line before the reset is safe.
        print("[FATAL] Core 0 unrecoverable exception in operational runtime: {} - resetting".format(err))
        machine.reset()


if __name__ == "__main__":
    main()
