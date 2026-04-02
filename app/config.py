"""
Config loader for mind-span-ce.

Parses the four-layer config shape:
  resources   = WHERE  — backend endpoint, auth, provider plugin
  sessions    = HOW    — conversation management, system prompts, history
  roles       = WHAT   — plugins, capabilities, context injection
  identities  = WHO    — token → identity → role → session → resource

Builds a flat token → IdentityContext lookup at startup.
Falls back gracefully to .env zero-config mode if config.yml is absent.

See notes/CONTEXT-SCHEMA.md for the ctx dict contract (used at request time).
See notes/config-idea.yml for the target config shape.
"""

import logging
import os
import re
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yml")

_TOKEN_MAP: dict[str, "IdentityContext"] = {}
_RAW_CONFIG: dict = {}
_config_loaded = False


@dataclass
class IdentityContext:
    # Identity layer
    identity_key: str
    identity_name: str | None       # context.name → <caller>
    identity_trust: str | None      # context.trust e.g. "trusted", "public"
    client_mode: str                # "raw" | "librechat" | "openwebui"

    # Role layer
    role_key: str
    role_model: str | None
    role_session: str | None        # None = backend owns session

    # Resource layer
    resource_key: str
    endpoint_url: str
    endpoint_token: str

    # Pre-resolved plugin lists for fast dispatch at request time
    # Each entry: (plugin_name, plugin_config_dict)
    identity_context_plugins: list = field(default_factory=list)
    role_context_plugins: list = field(default_factory=list)


def _expand_env_vars(obj):
    """Recursively expand ${VAR} in string values."""
    if isinstance(obj, str):
        return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ""), obj)
    elif isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_env_vars(i) for i in obj]
    return obj


def _extract_plugin_list(plugins_block) -> list[tuple[str, dict]]:
    """
    Convert a plugins: block from config into an ordered list of (name, config) tuples.

    Handles:
      plugins:
        time_inject:
          timezone: "Australia/Adelaide"
        memory_recall:
          enabled: true
    → [("time_inject", {"timezone": "..."}), ("memory_recall", {"enabled": True})]
    """
    if not plugins_block or not isinstance(plugins_block, dict):
        return []
    result = []
    for name, cfg in plugins_block.items():
        if cfg is None:
            cfg = {}
        elif not isinstance(cfg, dict):
            cfg = {}
        result.append((name, cfg))
    return result


def load_config() -> None:
    """Parse config.yml and populate the token lookup. Called once at startup."""
    global _config_loaded, _RAW_CONFIG

    if not os.path.exists(CONFIG_PATH):
        logger.warning(f"Config file not found at {CONFIG_PATH} — running in .env fallback mode.")
        return

    try:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to parse config file {CONFIG_PATH}: {e} — running in .env fallback mode.")
        return

    if not raw:
        logger.warning(f"Config file {CONFIG_PATH} is empty — running in .env fallback mode.")
        return

    try:
        raw = _expand_env_vars(raw)
        _RAW_CONFIG = raw
        _build_identity_map(raw)
        _config_loaded = True
        logger.info(f"Config loaded: {len(_TOKEN_MAP)} identity token(s) registered.")
    except Exception as e:
        logger.warning(f"Config structure error in {CONFIG_PATH}: {e} — running in .env fallback mode.")


def _build_identity_map(raw: dict) -> None:
    server = raw.get("server", {})
    resources = server.get("resources", {})
    roles = server.get("roles", {})
    identities = server.get("identities", {})

    seen_tokens: dict[str, str] = {}  # token → identity_key (for duplicate detection)

    for identity_key, identity_cfg in identities.items():
        if not identity_cfg:
            logger.warning(f"Identity '{identity_key}' has no config — skipping.")
            continue

        token = identity_cfg.get("token")
        if not token:
            logger.warning(f"Identity '{identity_key}' has no token — skipping.")
            continue

        if token in seen_tokens:
            logger.warning(
                f"Duplicate token for identity '{identity_key}' — already registered to '{seen_tokens[token]}'. Skipping."
            )
            continue
        seen_tokens[token] = identity_key

        role_key = identity_cfg.get("role")
        if not role_key:
            logger.warning(f"Identity '{identity_key}' has no role — skipping.")
            continue

        role_cfg = roles.get(role_key)
        if not role_cfg:
            logger.warning(f"Identity '{identity_key}' references unknown role '{role_key}' — skipping.")
            continue

        resource_key = role_cfg.get("resource")
        if not resource_key:
            logger.warning(f"Role '{role_key}' has no resource — skipping identity '{identity_key}'.")
            continue

        resource_cfg = resources.get(resource_key)
        if not resource_cfg:
            logger.warning(f"Role '{role_key}' references unknown resource '{resource_key}' — skipping identity '{identity_key}'.")
            continue

        # Resolve endpoint from resource
        oai = resource_cfg.get("endpoint", {}).get("plugins", {}).get("OpenAI-Provider", {})
        if not oai:
            logger.warning(f"Resource '{resource_key}' has no OpenAI-Provider plugin config — skipping identity '{identity_key}'.")
            continue

        endpoint_url = oai.get("url")
        endpoint_token = oai.get("token")
        if not endpoint_url or not endpoint_token:
            logger.warning(f"Resource '{resource_key}' OpenAI-Provider missing url or token — skipping identity '{identity_key}'.")
            continue

        # Context block (short-form sugar expanded here)
        identity_context = identity_cfg.get("context", {}) or {}
        identity_name = identity_context.get("name")
        identity_trust = identity_context.get("trust")

        # Build identity-level plugin list
        # If name/trust are set, prepend caller_inject automatically (short-form sugar)
        identity_plugins_raw = _extract_plugin_list(identity_context.get("plugins"))
        if identity_name and not any(name == "caller_inject" for name, _ in identity_plugins_raw):
            caller_config = {}
            if identity_name:
                caller_config["name"] = identity_name
            if identity_trust:
                caller_config["trust"] = identity_trust
            identity_plugins = [("caller_inject", caller_config)] + identity_plugins_raw
        else:
            identity_plugins = identity_plugins_raw

        # Build role-level plugin list (role.context.plugins)
        role_context = role_cfg.get("context", {}) or {}
        role_plugins = _extract_plugin_list(role_context.get("plugins"))

        ctx = IdentityContext(
            identity_key=identity_key,
            identity_name=identity_name,
            identity_trust=identity_trust,
            client_mode=identity_cfg.get("client_mode", "raw"),
            role_key=role_key,
            role_model=role_cfg.get("model"),
            role_session=role_cfg.get("session"),
            resource_key=resource_key,
            endpoint_url=endpoint_url,
            endpoint_token=endpoint_token,
            identity_context_plugins=identity_plugins,
            role_context_plugins=role_plugins,
        )
        _TOKEN_MAP[token] = ctx
        logger.debug(f"Registered token for '{identity_key}' → role '{role_key}' → resource '{resource_key}'")


def get_context_for_token(token: str) -> "IdentityContext | None":
    return _TOKEN_MAP.get(token)


def get_full_config() -> dict:
    """Returns the raw parsed config dict (env vars already expanded)."""
    return _RAW_CONFIG


def is_config_loaded() -> bool:
    return _config_loaded
