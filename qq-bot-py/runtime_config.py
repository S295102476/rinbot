"""Public deployment defaults and startup validation (no plugin side effects)."""
from pathlib import Path
from urllib.parse import urlsplit
import yaml

FEATURE_DEFAULTS = {
    "chat": True, "console": True, "persona": True, "meme": True,
    "sign_in": True, "minigames": True, "web_search": False,
    "image_gen": False, "nai": False, "pixiv": False, "link_parser": False,
    "gbvsr": False, "gsuid": False, "board_engines": False,
    "development_agent": False,
}


def load_config(path="config.yaml"):
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("Missing config.yaml: run docker compose run --rm init or create it from config.example.yaml.") from None
    except yaml.YAMLError:
        raise ValueError("Invalid config.yaml: check YAML indentation. Configuration values are not logged.") from None
    if not isinstance(value, dict):
        raise ValueError("config.yaml must be a mapping.")
    return value


def feature_enabled(config, name):
    return bool((config.get("features") or {}).get(name, FEATURE_DEFAULTS.get(name, False)))


def validate_config(config):
    required = ["database.host", "database.port", "database.user", "database.password",
                "database.database", "redis.host", "redis.port", "meme.minio.endpoint",
                "meme.minio.access_key", "meme.minio.secret_key", "meme.minio.bucket"]
    if feature_enabled(config, "chat"):
        required += ["ai.api_url", "ai.api_key", "ai.model"]
    missing = []
    for field in required:
        value = config
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is None or value == "":
            missing.append(field)
    if missing:
        raise ValueError("Missing required configuration: " + ", ".join(missing))
    protocol = (config.get("ai") or {}).get("protocol", "chat_completions")
    if protocol not in {"chat_completions", "gemini_native"}:
        raise ValueError("ai.protocol must be chat_completions or gemini_native.")
    if feature_enabled(config, "chat") and urlsplit(config["ai"]["api_url"]).scheme not in {"http", "https"}:
        raise ValueError("ai.api_url must be a complete HTTP(S) endpoint.")
    if not isinstance(config.get("allowed_groups"), list) or not config["allowed_groups"]:
        raise ValueError("allowed_groups must contain at least one group ID.")
    for gid in config["allowed_groups"]:
        if isinstance(gid, bool) or not str(gid).isdigit() or int(gid) <= 0:
            raise ValueError("allowed_groups must be a list of positive integer IDs.")


def enabled_plugins(config):
    result = ["normalizer", "group_mode", "dev_scope", "blocklist", "help", "recall",
              "marry", "deer", "coc_dice"]
    groups = {
        "chat": ["ai_chat", "group_chat", "memory", "translate", "mute_control", "poke_reaction", "agent_followup"],
        "persona": ["duty_roster", "agent_plugin"],
        "console": ["admin_console", "meme_admin"],
        "meme": ["meme_collector"], "sign_in": ["sign_in"], "minigames": ["minigames"],
        "web_search": ["search", "web_search"], "image_gen": ["image_gen"], "nai": ["nai"],
        "pixiv": ["setu"], "link_parser": ["link_parser"], "gbvsr": ["gbvsr_frame", "gb_usage_rate"],
        "gsuid": ["gsuid_bridge"],
    }
    for name, modules in groups.items():
        if feature_enabled(config, name):
            if name == "gsuid" and not (config.get("gsuid") or {}).get("enabled", False):
                continue
            result.extend(modules)
    if feature_enabled(config, "development_agent") and "agent_plugin" not in result:
        result.append("agent_plugin")
    return result
