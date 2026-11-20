"""Garbage-collector set-up for a long-running service.

Once the service has started, the objects of every imported library (modules,
classes, functions, parsers: about 100,000) live until it stops. ``after_startup``
collects once and then freezes them (``gc.freeze``), so later full collections do
not visit them again. Python's own documentation recommends this for servers.
Nothing frozen is lost: a frozen object that becomes unreachable through reference
counting is still freed.
"""

from __future__ import annotations

import gc


def after_startup() -> int:
    """Collect, then freeze what is alive; the number of objects frozen."""
    gc.collect()
    gc.freeze()
    return gc.get_freeze_count()
