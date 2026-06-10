"""Tiny hierarchical perf timer for instrumenting editor navigation.

Usage:
    from ._perf import span, report_and_clear
    with span("foo"):
        with span("bar"):
            ...
    report_and_clear()  # prints a tree, clears state

Toggle ENABLED to False to silence everything (zero-cost no-op contexts).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import List, Tuple

ENABLED = False

# Each node is (name, elapsed_ms, children). The stack holds in-flight nodes
# whose children list is still being filled; root nodes graduate to _root once
# their span exits at depth 0.
_stack: List[Tuple[str, float, list]] = []
_root: List[Tuple[str, float, list]] = []


@contextmanager
def span(name: str):
    if not ENABLED:
        yield
        return
    start = time.perf_counter()
    children: list = []
    _stack.append((name, start, children))
    try:
        yield
    finally:
        name_, start_, children_ = _stack.pop()
        elapsed_ms = (time.perf_counter() - start_) * 1000.0
        node = (name_, elapsed_ms, children_)
        if _stack:
            _stack[-1][2].append(node)
        else:
            _root.append(node)


def report_and_clear() -> None:
    if not ENABLED or not _root:
        return
    lines: List[str] = []

    def fmt(node, depth):
        name, ms, children = node
        lines.append(f"{'  ' * depth}{name}  {ms:.1f}ms")
        for child in children:
            fmt(child, depth + 1)

    for n in _root:
        fmt(n, 0)
    print("[perf]\n" + "\n".join(lines), flush=True)
    _root.clear()
