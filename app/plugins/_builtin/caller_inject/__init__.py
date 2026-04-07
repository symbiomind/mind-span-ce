"""
caller_inject — builtin plugin

Injects <caller> tag into bridge_context based on the identity's name and trust level.

Hook points: identity.context, role.context

Config (all optional — falls back to identity context values from ctx):
  name:   str   — display name for this caller (e.g. "Martin")
  trust:  str   — trust level (e.g. "trusted", "public", "system", "operator")

Uses _raw_ prefix so the value is injected verbatim into <bridge_context>
without an extra wrapping tag.

Short-form sugar in config.yml:
  identities:
    Martin_crabby:
      context:
        name: Martin
        trust: trusted

The pipeline auto-prepends caller_inject with {name, trust} when
context.name is set, so this plugin runs transparently from short-form config.
"""

from app.context import PipelineCtx

SUPPORTED_HOOKS = ["identity.context", "role.context"]


def hook(hook_point: str, ctx: PipelineCtx, config: dict) -> PipelineCtx | None:
    name = config.get("name") or ctx.identity.name
    trust = config.get("trust") or ctx.identity.trust

    if not name:
        return None

    if trust:
        caller_xml = f'<caller trust="{trust}">{name}</caller>'
    else:
        caller_xml = f"<caller>{name}</caller>"

    ctx.bridge_context["_raw_caller"] = caller_xml
    return ctx
