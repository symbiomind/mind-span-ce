"""
Plugin dispatcher for mind-span-ce v0.2.

Fires plugin hooks in config-declaration order (the order they appear in the
plugin_list). Config order IS the execution order — no priority numbers.

Plugin interface:
  plugin.hook(hook_point: str, ctx: PipelineCtx | StartupCtx, config: dict) -> PipelineCtx | StartupCtx | None

Behaviour:
  - Plugin not in registry → logged warning, skip
  - Hook point not in plugin's SUPPORTED_HOOKS → logged warning, skip
  - Plugin raises exception → logged error, fail open, continue to next plugin
  - Plugin returns None → ctx unchanged, continue
  - Plugin returns ctx → replace ctx, continue

A single plugin can support any number of hook points — including all of them.
The hook_point argument tells the plugin which stage it's currently in.

See notes/PLUGIN-DESIGN.md for the full plugin authoring contract.
"""

import logging
from typing import Union

from .context import PipelineCtx, StartupCtx
from . import plugin_loader

logger = logging.getLogger(__name__)

Ctx = Union[PipelineCtx, StartupCtx]


def dispatch(
    hook_point: str,
    ctx: Ctx,
    plugin_list: list[tuple[str, dict]],
) -> Ctx:
    """
    Fire each plugin in plugin_list at the given hook_point.

    plugin_list: list of (plugin_name, plugin_config) tuples in config-declaration order.
    ctx: PipelineCtx for per-request hooks, StartupCtx for server.startup hook.

    Returns the final ctx after all plugins have run.
    Fails open — plugin errors are logged, pipeline continues unaffected.
    """
    for plugin_name, plugin_config in plugin_list:
        plugin = plugin_loader.get_plugin(plugin_name)
        if plugin is None:
            logger.warning(
                f"[{hook_point}] Plugin '{plugin_name}' not found in registry — "
                f"is it installed in plugins/_builtin/ or plugins/user/? Skipping."
            )
            continue

        supported = getattr(plugin, "SUPPORTED_HOOKS", [])
        if hook_point not in supported:
            logger.warning(
                f"[{hook_point}] Plugin '{plugin_name}' does not support this hook "
                f"(supports: {supported}) — skipping."
            )
            continue

        try:
            result = plugin.hook(hook_point, ctx, plugin_config)
            if result is not None:
                ctx = result
        except Exception as e:
            logger.error(
                f"[{hook_point}] Plugin '{plugin_name}' raised an exception: {e}",
                exc_info=True,
            )
            # Fail open — log and continue to next plugin

    return ctx
