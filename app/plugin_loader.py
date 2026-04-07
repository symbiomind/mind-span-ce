"""
Plugin loader for mind-span-ce v0.2.

Scans plugin directories at startup and loads plugins into the registry.
Separated from dispatch — this module only loads, never calls.

Plugin structure:
  my_plugin/
    __init__.py       ← exports SUPPORTED_HOOKS and hook()
    requirements.txt  ← optional, auto-pip-installed on startup
    README.md         ← optional, documents hook points and config schema

Load order:
  1. _builtin/ plugins (alphabetical)
  2. user/ plugins (alphabetical)

Builtins load first so user plugins with the same name are warned and skipped.
(User plugins cannot silently shadow builtins — rename your directory.)

See notes/PLUGIN-DESIGN.md for the plugin authoring contract.
"""

import importlib.util
import logging
import os
import subprocess
import sys
from types import ModuleType

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, ModuleType] = {}


def load_plugins(builtin_dir: str, user_dir: str) -> None:
    """
    Load all plugins from both directories into the registry.
    Called once at startup.
    """
    _load_from_dir(builtin_dir, source="builtin")
    _load_from_dir(user_dir, source="user")
    loaded = sorted(_REGISTRY.keys())
    logger.info(
        f"Plugin registry: {len(_REGISTRY)} plugin(s) loaded: {loaded if loaded else '(none)'}"
    )


def get_plugin(name: str) -> ModuleType | None:
    """Returns the loaded plugin module for the given name, or None."""
    return _REGISTRY.get(name)


def get_registry() -> dict[str, ModuleType]:
    """Returns the loaded plugin registry (read-only reference)."""
    return _REGISTRY


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

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
            logger.debug(f"Skipping '{entry.name}' in {source}/ — no __init__.py")
            continue
        _load_plugin(entry.name, entry.path, init_path, source)


def _load_plugin(name: str, plugin_dir: str, init_path: str, source: str) -> None:
    if source == "user" and name in _REGISTRY:
        logger.warning(
            f"User plugin '{name}' conflicts with a builtin of the same name — "
            f"skipping user plugin. Rename your plugin directory to use a unique name."
        )
        return

    # Auto-install requirements.txt if present
    req_path = os.path.join(plugin_dir, "requirements.txt")
    if os.path.isfile(req_path):
        logger.info(f"Installing requirements for {source} plugin '{name}'...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-r", req_path, "-q"],
                timeout=120,
            )
        except Exception as e:
            logger.error(
                f"Failed to install requirements for {source} plugin '{name}': {e} — "
                f"plugin may not work correctly."
            )

    try:
        spec = importlib.util.spec_from_file_location(f"plugins.{source}.{name}", init_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"plugins.{source}.{name}"] = module
        spec.loader.exec_module(module)
        _REGISTRY[name] = module
        hooks = getattr(module, "SUPPORTED_HOOKS", [])
        logger.debug(f"Loaded {source} plugin '{name}' — supported hooks: {hooks}")
    except Exception as e:
        logger.error(f"Failed to load {source} plugin '{name}' from '{init_path}': {e}")
