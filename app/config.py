"""
Config loader for mind-span-ce v0.2.

Parses config.yml into raw config blocks accessible at request time.
The four-layer config shape:

  resources   = WHERE  — backend endpoint definition
  sessions    = HOW    — conversation management (optional)
  roles       = WHAT   — plugins, capabilities, context injection
  identities  = WHO    — token → identity → roles → resource

Key design decisions vs v0.1:
  - No IdentityContext dataclass — config is stored as raw dicts, context assembled at request time
  - No endpoint pre-resolution — url/token are populated by resource.endpoint plugin at request time
  - _TOKEN_MAP maps token → identity_key (string), not to a pre-built context object
  - roles: [list] is the new shape (was single role:) — resolve_roles() returns a list
  - name/trust sugar expansion happens in the pipeline, not here

See docs/config.yml/README.md for the full config shape.
"""

import logging
import os
import re

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yml")

_SERVER_CFG: dict = {}           # raw server: block, env vars expanded
_TOKEN_MAP: dict[str, str] = {}  # bearer_token → identity_key
_config_loaded: bool = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config() -> None:
    """
    Parse config.yml and populate module state. Called once at startup.

    On missing file: logs info (valid — no config = /health only mode).
    On parse error: logs error, state remains unloaded.
    On empty file: logs warning, state remains unloaded.
    """
    global _config_loaded, _SERVER_CFG, _TOKEN_MAP

    if not os.path.exists(CONFIG_PATH):
        logger.info(
            f"No config file found at {CONFIG_PATH} — "
            f"server will serve /health only. Create a config.yml to enable routing."
        )
        return

    try:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to parse config file {CONFIG_PATH}: {e}")
        return

    if not raw:
        logger.warning(f"Config file {CONFIG_PATH} is empty — serving /health only.")
        return

    try:
        raw = _expand_env_vars(raw)
        server = raw.get("server", {})
        if not server:
            logger.warning(f"Config file {CONFIG_PATH} has no 'server:' block — serving /health only.")
            return

        token_map = _build_token_map(server)
        _SERVER_CFG = server
        _TOKEN_MAP = token_map
        _config_loaded = True
        logger.info(f"Config loaded: {len(_TOKEN_MAP)} identity token(s) registered.")
    except Exception as e:
        logger.error(f"Config structure error in {CONFIG_PATH}: {e}")


def is_config_loaded() -> bool:
    return _config_loaded


def get_identity_key_for_token(token: str) -> str | None:
    """Returns the identity key for a bearer token, or None if not found."""
    return _TOKEN_MAP.get(token)


def get_server_cfg() -> dict:
    """Returns the full parsed server: block (env vars already expanded)."""
    return _SERVER_CFG


def resolve_identity(key: str) -> dict | None:
    """Returns the identity config block for a given key, or None."""
    return _SERVER_CFG.get("identities", {}).get(key)


def resolve_role(key: str) -> dict | None:
    """Returns the role config block for a given key, or None."""
    return _SERVER_CFG.get("roles", {}).get(key)


def resolve_resource(key: str) -> dict | None:
    """Returns the resource config block for a given key, or None."""
    return _SERVER_CFG.get("resources", {}).get(key)


def resolve_session(key: str) -> dict | None:
    """Returns the session config block for a given key, or None. Sessions are optional."""
    return _SERVER_CFG.get("sessions", {}).get(key)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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

    Order is preserved — this IS the execution order.
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


def _get_identity_roles(identity_cfg: dict, identity_key: str) -> list[str]:
    """
    Extract the list of role keys from an identity config.

    Supports both new shape (roles: [list]) and old shape (role: single).
    Logs a warning for multi-role identities (only first will be used in v0.2).
    """
    # New shape: roles: [role_a, role_b]
    roles = identity_cfg.get("roles")
    if isinstance(roles, list):
        if len(roles) > 1:
            logger.warning(
                f"Identity '{identity_key}' declares {len(roles)} roles — "
                f"only the first role ('{roles[0]}') is used in v0.2 "
                f"(multi-role fan-out is planned for a future version)."
            )
        return [r for r in roles if r]

    # Old shape: role: single_role (backwards compat, log a hint)
    role = identity_cfg.get("role")
    if role:
        logger.debug(
            f"Identity '{identity_key}' uses old 'role:' key — "
            f"consider updating to 'roles: [{role}]'."
        )
        return [role]

    return []


def _build_token_map(server: dict) -> dict[str, str]:
    """
    Build the token → identity_key map from the server config block.
    Invalid identities are excluded with warnings — startup always continues.
    """
    resources = server.get("resources", {}) or {}
    roles = server.get("roles", {}) or {}
    identities = server.get("identities", {}) or {}

    token_map: dict[str, str] = {}
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
                f"Identity '{identity_key}' has a duplicate token (already registered to "
                f"'{seen_tokens[token]}') — skipping."
            )
            continue

        role_keys = _get_identity_roles(identity_cfg, identity_key)
        if not role_keys:
            logger.warning(f"Identity '{identity_key}' has no roles — skipping.")
            continue

        # Validate the first role (the one that will actually be used)
        role_key = role_keys[0]
        role_cfg = roles.get(role_key)
        if not role_cfg:
            logger.warning(
                f"Identity '{identity_key}' references unknown role '{role_key}' — skipping."
            )
            continue

        resource_key = role_cfg.get("resource")
        if not resource_key:
            logger.warning(
                f"Role '{role_key}' (used by identity '{identity_key}') has no resource — skipping."
            )
            continue

        if resource_key not in resources:
            logger.warning(
                f"Role '{role_key}' references unknown resource '{resource_key}' — skipping identity '{identity_key}'."
            )
            continue

        seen_tokens[token] = identity_key
        token_map[token] = identity_key
        logger.debug(f"Registered token for '{identity_key}' → role '{role_key}' → resource '{resource_key}'")

    return token_map
