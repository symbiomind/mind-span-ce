"""
Hook registry — WordPress-style action/filter system.

Actions: fire(event, *args) — notify, return value ignored
Filters: apply(event, value, *args) — transform, return modified value

Priority: lower number = runs first (default 10)
replace=True: clears all existing handlers on that event before registering
"""

from collections import defaultdict
import logging

logger = logging.getLogger(__name__)

_actions: dict[str, list[tuple[int, callable]]] = defaultdict(list)
_filters: dict[str, list[tuple[int, callable]]] = defaultdict(list)


def on(event: str, priority: int = 10, replace: bool = False):
    """Decorator to register an action hook."""
    def decorator(fn):
        register_action(event, fn, priority=priority, replace=replace)
        return fn
    return decorator


def filter_hook(event: str, priority: int = 10, replace: bool = False):
    """Decorator to register a filter hook."""
    def decorator(fn):
        register_filter(event, fn, priority=priority, replace=replace)
        return fn
    return decorator


def register_action(event: str, fn: callable, priority: int = 10, replace: bool = False):
    if replace:
        _actions[event].clear()
    _actions[event].append((priority, fn))
    _actions[event].sort(key=lambda x: x[0])
    logger.debug(f"Action registered: {event} → {fn.__name__} (priority={priority})")


def register_filter(event: str, fn: callable, priority: int = 10, replace: bool = False):
    if replace:
        _filters[event].clear()
    _filters[event].append((priority, fn))
    _filters[event].sort(key=lambda x: x[0])
    logger.debug(f"Filter registered: {event} → {fn.__name__} (priority={priority})")


def unregister_action(event: str, fn: callable):
    _actions[event] = [(p, f) for p, f in _actions[event] if f != fn]


def unregister_filter(event: str, fn: callable):
    _filters[event] = [(p, f) for p, f in _filters[event] if f != fn]


async def fire(event: str, *args, **kwargs):
    """Fire an action — call all registered handlers, ignore return values."""
    for _, fn in _actions.get(event, []):
        try:
            import asyncio
            if asyncio.iscoroutinefunction(fn):
                await fn(*args, **kwargs)
            else:
                fn(*args, **kwargs)
        except Exception as e:
            logger.error(f"Hook error in {event} → {fn.__name__}: {e}")


async def apply(event: str, value, *args, **kwargs):
    """Apply a filter — each handler transforms value, final result returned."""
    for _, fn in _filters.get(event, []):
        try:
            import asyncio
            if asyncio.iscoroutinefunction(fn):
                value = await fn(value, *args, **kwargs)
            else:
                value = fn(value, *args, **kwargs)
        except Exception as e:
            logger.error(f"Filter error in {event} → {fn.__name__}: {e}")
    return value
