"""
Config-driven plugin dispatch engine for mind-span-ce.

Replaces the decorator-based hooks.py system.

Design: see notes/PLUGIN-DESIGN.md
ctx contract: see notes/CONTEXT-SCHEMA.md

Plugins are loaded from two directories at startup:
  app/plugins/_builtin/   — shipped with image, read-only
  plugins/user/           — bind-mounted by user

Each plugin is a directory containing __init__.py that exports:
  SUPPORTED_HOOKS: list[str]
  hook(hook_point: str, ctx: dict, config: dict) -> dict | None

dispatch() fires plugins in config-declaration order (the order they appear
in the plugin_list). This IS the priority system — config order = execution order.
"""

import importlib.util
import logging
import os
import sys
from types import ModuleType

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, ModuleType] = {}


def load_plugins(builtin_dir: str, user_dir: str) -> None:
    """
    Load all plugins from both directories into the registry.
    Called once at startup. Registry key = plugin directory name.
    Builtin plugins loaded first; user plugins can override by using the same name.
    """
    _load_from_dir(builtin_dir, source="builtin")
    _load_from_dir(user_dir, source="user")
    logger.info(f"Plugin registry: {len(_REGISTRY)} plugin(s) loaded: {sorted(_REGISTRY.keys())}")


def _load_from_dir(plugin_dir: str, source: str) -> None:
    if not os.path.isdir(plugin_dir):
        logger.debug(f"Plugin dir '{plugin_dir}' not found — skipping.")
        return

    for entry in sorted(os.scandir(plugin_dir), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue
        init_path = os.path.join(entry.path, "__init__.py")
        if not os.path.isfile(init_path):
            logger.debug(f"Skipping '{entry.name}' — no __init__.py")
            continue
        _load_plugin(entry.name, init_path, source)


def _load_plugin(name: str, init_path: str, source: str) -> None:
    if source == "user" and name in _REGISTRY:
        logger.warning(
            f"User plugin '{name}' conflicts with a builtin of the same name — skipping user plugin. "
            f"Rename your plugin directory to override this behaviour."
        )
        return
    try:
        spec = importlib.util.spec_from_file_location(f"plugins.{source}.{name}", init_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"plugins.{source}.{name}"] = module
        spec.loader.exec_module(module)
        _REGISTRY[name] = module
        hooks = getattr(module, "SUPPORTED_HOOKS", [])
        logger.debug(f"Loaded {source} plugin '{name}' — hooks: {hooks}")
    except Exception as e:
        logger.error(f"Failed to load {source} plugin '{name}' from '{init_path}': {e}")


def dispatch(hook_point: str, ctx: dict, plugin_list: list) -> dict:
    """
    Fire each plugin in plugin_list at the given hook_point.

    plugin_list: list of (plugin_name: str, plugin_config: dict) tuples
                 In config-declaration order — this IS the execution order.

    Returns the final ctx after all plugins have run.
    Fail open: plugin errors are logged, pipeline continues unaffected.
    """
    for plugin_name, plugin_config in plugin_list:
        plugin = _REGISTRY.get(plugin_name)
        if plugin is None:
            logger.warning(f"Plugin '{plugin_name}' not found in registry — is it installed? Skipping.")
            continue

        supported = getattr(plugin, "SUPPORTED_HOOKS", [])
        if hook_point not in supported:
            logger.warning(
                f"Plugin '{plugin_name}' has no handler for hook '{hook_point}' "
                f"(supports: {supported}) — skipping."
            )
            continue

        try:
            result = plugin.hook(hook_point, ctx, plugin_config)
            if result is not None:
                ctx = result
        except Exception as e:
            logger.error(
                f"Plugin '{plugin_name}' raised an exception at hook '{hook_point}': {e}",
                exc_info=True,
            )
            # Fail open — continue to next plugin

    return ctx


def get_registry() -> dict[str, ModuleType]:
    """Returns the loaded plugin registry (read-only reference)."""
    return _REGISTRY
