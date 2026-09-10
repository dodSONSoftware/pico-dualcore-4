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
    # A bus property (like the board heap thresholds), read before the per-core
    # split discards the full config: the outbound queue's deterministic count
    # ceiling, subordinate to the heap policy.
    outbound_queue_max_messages = config["outbound_queue_max_messages"]
    core0_config, core1_config = split_config(config)
    config = None

    # The bus is heap-governed: its admission thresholds are the board-specific
    # preferred free-heap reserve (start of pressure handling) and the hard
    # survival floor (hardware.py is the single source of truth), shared by
    # both queues through one heap-admission lock, plus a configured count
    # ceiling on the outbound queue.
    intercore = InterCore(
        minimum_free_heap_bytes=hardware["minimum_free_heap_bytes"],
        preferred_free_heap_bytes=hardware["preferred_free_heap_bytes"],
        outbound_queue_max_messages=outbound_queue_max_messages,
    )
    hardware = None

    # One runtime ID for this boot; both cores must agree on it.
    runtime_id = _runtime_id()

    # Core 1's worker thread is spawned here, first -- before the core1 and
    # core0 imports and the network bring-up: the thread's default ~4 KiB
    # stack needs one contiguous GC-pool run, and on the Pico W (256 KB) no
    # such run survives the import sets plus the CYW43/lwIP buffers (a spawn
    # that late raised MemoryError into the silent reset boundary below and
    # reboot-looped the board). Spawning before the core1 import gives the
    # stack the cleanest pool state of the startup -- the core1 chain's code
    # objects are not yet interleaved into the pool, the state the Pico W
    # validated in 0.4.87. The worker stays idle until Core 0's network
    # snapshot reports the full startup contract verified, then runs
    # core1_main -- Core 1 is still gated on the startup contract (its modules
    # are already loaded by then, so the ready path is a sys.modules cache
    # hit, not a parse).
    # start_new_thread(func, args, kwargs): the third positional argument is
    # the keyword-args dict forwarded to the thread function itself -- this
    # MicroPython _thread has no stack-size parameter, so the thread always
    # runs on the MicroPython-default stack.
    import _thread

    def _core1_thread_entry(bus, cfg, boot_ms, rid):
        # Stamp liveness at spawn so a thread death in the wait is bounded by
        # Core 0's stale-heartbeat watchdog (armed at the end of core0.start())
        # instead of leaving Core 0 running with no Core 1.
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

    # Core 1's modules are imported here, on the main thread, after the spawn
    # and BEFORE the core0 import and the network bring-up. Importing core1
    # parses its source, and that parse working buffer is a C-heap (non-GC)
    # allocation; by the time the network snapshot reports ready the CYW43/
    # lwIP buffers plus the worker's ~4 KiB stack have consumed the C heap, so
    # a late parse MemoryErrors on a ~2 KiB buffer while gc.mem_free() still
    # shows ~102 KiB (gc.collect() compacts only the GC heap and cannot
    # recover the C heap). Importing now, while the C heap is still clear,
    # keeps the worker's ready path to a sys.modules cache hit. core1_main
    # still RUNS on the worker thread, so Core 1's device ownership is
    # unchanged; the modules have no import-time side effects, so loading them
    # here is safe.
    from core1 import core1_main
    print("[INFO] Core 1 modules imported pre-network (free heap {} bytes)".format(gc.mem_free()))

    # Reclaim before the heaviest import of the startup. Since the collect at
    # the top of main() the pool has accumulated collectable garbage -- the
    # full configuration graph (nulled after the per-core split), the
    # configuration-recovery parse residue, both import sets' compile
    # temporaries, and the thread-spawn residue -- interleaved between the
    # live import objects. On the Pico W the 0.4.90 core0 import died exactly
    # here: a 1336-byte allocation in the import machinery with 88,176 bytes
    # of heap free (the pool fragmented into no contiguous run), and this
    # import sits ABOVE the recovery boundary below, so the MemoryError
    # escaped to the silent reset boundary and reboot-looped the board. The
    # collect coalesces the freed runs before the import's code-object
    # allocations; the boot line reports the post-collect free heap so a
    # recurrence shows whether the pool is exhausted or merely interleaved.
    # gc.collect() coalesces free runs but does not compact live objects (the
    # documented 0.4.74 lesson) -- if the run still does not exist, the next
    # step is a smaller resident import set, not more collection.
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

    # Recovery boundary for the operational phase. Everything above is
    # deterministic startup validation that intentionally fails fast: a
    # misconfigured or unsupported board must stay down with a diagnosable
    # error, not reboot forever. From here on every layer has a supervisor:
    # Core 0's heartbeat watchdog recovers a dead Core 1, and the hardware
    # watchdog (armed at the end of core0.start()) recovers a Core 0 that is
    # alive but no longer making progress — the exception boundary below
    # covers what a reset cannot: an unrecoverable Core 0 exception, which is
    # a controlled board reset, not application termination.
    try:
        # Core 0 establishes the network before Core 1's modules are imported:
        # the worker spawned above imports core1 only after the network
        # snapshot reports the startup contract verified.
        core0.start()

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
