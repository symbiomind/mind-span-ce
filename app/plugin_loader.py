"""
Plugin loader — scans plugins/ directory and loads each plugin module.

Structure:
  plugins/
    _builtin/
      request_logger/
        __init__.py
        requirements.txt  (optional)
    user/                 (bind-mounted by user)
      my_plugin/
        __init__.py

Each plugin's __init__.py self-registers via hooks decorators at import time.
Builtin plugins load first, then user plugins.
"""

import importlib
import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGINS_DIR = Path(__file__).parent / "plugins"


def _install_requirements(plugin_dir: Path):
    req_file = plugin_dir / "requirements.txt"
    if req_file.exists():
        logger.info(f"Installing requirements for plugin: {plugin_dir.name}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(req_file), "-q"]
        )


def _load_plugin(plugin_dir: Path):
    init_file = plugin_dir / "__init__.py"
    if not init_file.exists():
        return

    _install_requirements(plugin_dir)

    # Build a dotted module name: app.plugins._builtin.request_logger
    rel = plugin_dir.relative_to(Path(__file__).parent)
    module_name = "app." + ".".join(rel.parts)

    try:
        spec = importlib.util.spec_from_file_location(module_name, init_file)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        logger.info(f"Loaded plugin: {module_name}")
    except Exception as e:
        logger.error(f"Failed to load plugin {module_name}: {e}")


def load_all():
    """Load builtin plugins first, then user plugins."""
    for tier in ["_builtin", "user"]:
        tier_dir = PLUGINS_DIR / tier
        if not tier_dir.exists():
            continue
        for plugin_dir in sorted(tier_dir.iterdir()):
            if plugin_dir.is_dir() and not plugin_dir.name.startswith("."):
                _load_plugin(plugin_dir)
