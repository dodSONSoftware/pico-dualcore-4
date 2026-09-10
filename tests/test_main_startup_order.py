# test_main_startup_order.py - main.py startup-order invariants (Pico W boot)
# Copyright (c) 2026 dodson Software ( dodson labs )
# SPDX-License-Identifier: MIT
#
# The Pico W (256 KB) boot made the order of the startup allocations in
# main.py load-bearing: 0.4.87 validated the worker spawn before the core0
# import (a late spawn's ~4 KiB stack found no contiguous GC-pool run and
# MemoryErrored into the silent reset boundary, reboot-looping the board), and
# 0.4.90 failed in the core0 import itself (a 1336-byte import-machinery
# allocation with 88,176 bytes of heap free -- a fragmented pool, no
# contiguous run -- escaping above the recovery boundary into the same
# reboot loop). Pin the order structurally so a refactor cannot move an
# allocation relative to the ones that fragment the pool around it.

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _main_function():
    tree = ast.parse((ROOT / "main.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("main.py defines no main()")


def _line_of(predicate, node):
    lines = [n.lineno for n in ast.walk(node) if predicate(n)]
    assert len(lines) == 1, (predicate, lines)
    return lines[0]


def _import_line(node, module):
    return _line_of(lambda n: isinstance(n, ast.ImportFrom) and n.module == module, node)


def test_worker_spawns_before_both_imports():
    # The thread's ~4 KiB stack is one contiguous GC-pool run; spawning after
    # an import set interleaves that set's code objects into the pool, and on
    # the Pico W the run no longer survives (0.4.87: the late spawn
    # MemoryError'd into the silent reset boundary). The spawn sits at the
    # cleanest pool state of the startup -- before both import sets.
    main = _main_function()
    spawn = _line_of(
        lambda n: isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "start_new_thread",
        main,
    )
    core1 = _import_line(main, "core1")
    core0 = _import_line(main, "core0")
    assert spawn < core1 < core0


def test_heap_reclaimed_before_the_core0_import():
    # The 0.4.90 Pico W failure: a 1336-byte import-machinery allocation with
    # 88,176 bytes free (a fragmented pool, no contiguous run). A gc.collect()
    # between the core1 import and the core0 import reclaims the startup
    # garbage accumulated since the boot collect (the nulled configuration
    # graph, the recovery parse residue, both import sets' compile
    # temporaries, the thread-spawn residue) and coalesces the free runs
    # before the import's code-object allocations.
    main = _main_function()
    core1 = _import_line(main, "core1")
    core0 = _import_line(main, "core0")
    collects = [
        n.lineno
        for n in ast.walk(main)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "collect"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "gc"
    ]
    assert any(core1 < line < core0 for line in collects)


def test_core0_import_stays_above_the_recovery_boundary():
    # The import is deterministic startup: fail-fast, outside the controlled
    # reset boundary that wraps core0.start()/run() (a MemoryError there is a
    # visible traceback + reset, not the allocation-free silent path).
    main = _main_function()
    core0 = _import_line(main, "core0")
    tries = [n.lineno for n in ast.walk(main) if isinstance(n, ast.Try)]
    assert tries and core0 < min(tries)
