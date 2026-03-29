"""
Config loader for mind-span-ce.

Loads config.yml at startup and builds a flat token → RequestContext lookup.
If config.yml is missing or unparseable, falls back gracefully (empty token map).
Pipeline then uses .env as fallback when ctx is None.
"""

import logging
import os
import re
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yml")

_TOKEN_MAP: dict[str, "RequestContext"] = {}
_config_loaded = False


@dataclass
class RequestContext:
    resource_id: str
    display_name: str
    role: str
    endpoint_url: str
    endpoint_token: str
    endpoint_model: str | None
    session_plugins: dict
    user_name: str
    session_key: str | None = None
    client_mode: str = "raw"


def _expand_env_vars(obj):
    """Recursively expand ${VAR} in string values."""
    if isinstance(obj, str):
        return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), ""), obj)
    elif isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_expand_env_vars(i) for i in obj]
    return obj


def load_config() -> None:
    """Parse config.yml and populate the token lookup. Called once at startup."""
    global _config_loaded

    if not os.path.exists(CONFIG_PATH):
        logger.warning(f"Config file not found at {CONFIG_PATH} — running in .env fallback mode.")
        return

    try:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        logger.warning(f"Failed to parse config file {CONFIG_PATH}: {e} — running in .env fallback mode.")
        return

    try:
        raw = _expand_env_vars(raw)
        _build_token_map(raw)
        _config_loaded = True
        logger.info(f"Config loaded: {len(_TOKEN_MAP)} user token(s) registered.")
    except Exception as e:
        logger.warning(f"Config structure error in {CONFIG_PATH}: {e} — running in .env fallback mode.")


def _build_token_map(raw: dict) -> None:
    resources = raw.get("server", {}).get("resource", {})
    for resource_id, resource_cfg in resources.items():
        resource_endpoint = resource_cfg.get("endpoint")
        roles = resource_cfg.get("roles", {})
        users = resource_cfg.get("users", {})

        for _user_key, user_cfg in users.items():
            role_name = user_cfg["role"]
            user_token = user_cfg["token"]
            user_name = user_cfg.get("name", _user_key)

            role_cfg = roles.get(role_name, {})

            # Resolve endpoint: role.endpoint → resource.endpoint
            # "default" (or None/absent) means use the resource-level endpoint
            role_endpoint = role_cfg.get("endpoint")
            if role_endpoint is None or role_endpoint == "default":
                endpoint_src = resource_endpoint
            else:
                endpoint_src = role_endpoint
            if not endpoint_src:
                logger.warning(
                    f"No endpoint found for user '{_user_key}' (role '{role_name}', resource '{resource_id}') — skipping."
                )
                continue

            oai = endpoint_src.get("plugins", {}).get("OpenAI-Provider", {})
            if not oai:
                logger.warning(
                    f"No OpenAI-Provider plugin config for user '{_user_key}' — skipping."
                )
                continue

            endpoint_url = oai.get("url")
            endpoint_token = oai.get("token")
            if not endpoint_url or not endpoint_token:
                logger.warning(
                    f"Missing url or token in OpenAI-Provider config for user '{_user_key}' — skipping."
                )
                continue

            ctx = RequestContext(
                resource_id=resource_id,
                display_name=user_name,
                role=role_name,
                endpoint_url=endpoint_url,
                endpoint_token=endpoint_token,
                endpoint_model=oai.get("model"),
                session_plugins=role_cfg.get("session", {}).get("plugins", {}),
                user_name=user_name,
                session_key=role_cfg.get("session_key"),
                client_mode=user_cfg.get("client_mode", "raw"),
            )
            _TOKEN_MAP[user_token] = ctx
            logger.debug(f"Registered token for user '{user_name}' → resource '{resource_id}', role '{role_name}'")


def get_context_for_token(token: str) -> "RequestContext | None":
    return _TOKEN_MAP.get(token)


def is_config_loaded() -> bool:
    return _config_loaded
