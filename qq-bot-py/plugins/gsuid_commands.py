"""Pure GsCore command recognition shared by routing and bridge plugins."""

GAME_PREFIXES = (
    "ww", "end", "zmd", "ark", "mrfz", "zzz", "绝区零", "lol", "sr", "nte", "ss",
)

CORE_COMMANDS = (
    "扫码登陆", "扫码登录",
    "绑定设备", "设备登录", "设备登陆",
    "mys设备登录", "mys设备登陆", "mys绑定设备",
    "core", "Core", "CORE",
)

DIRECT_ONLY_CORE_COMMANDS = (
    "绑定设备", "设备登录", "设备登陆",
    "mys设备登录", "mys设备登陆", "mys绑定设备",
)

DEVICE_COMMAND_ALIASES = {
    "绑定设备": "mys设备登录",
    "设备登录": "mys设备登录",
    "设备登陆": "mys设备登陆",
}


def _is_game_command(text: str) -> bool:
    """Return whether text belongs to a GsCore game or core command."""
    lower = text.lower().lstrip()
    if any(lower.startswith(prefix) for prefix in GAME_PREFIXES):
        return True
    stripped = text.strip()
    return any(
        stripped == command or stripped.startswith(command)
        for command in CORE_COMMANDS
    )


def _is_core_command(text: str) -> bool:
    """Return whether text is a command owned by GsCore itself."""
    stripped = text.strip()
    return any(
        stripped == command or stripped.startswith(command)
        for command in CORE_COMMANDS
    )


def _is_direct_only_core_command(text: str) -> bool:
    """Return whether text is a device command that requires a DM."""
    stripped = text.strip()
    for command in DIRECT_ONLY_CORE_COMMANDS:
        if stripped == command or stripped.startswith(command):
            return True
        if any(
            stripped == f"{prefix}{command}"
            or stripped.startswith(f"{prefix}{command}")
            for prefix in ("core", "Core", "CORE")
        ):
            return True
    return False


def _normalize_core_command_text(text: str) -> str:
    """Translate historical device aliases to GsCore's documented command."""
    stripped = text.strip()
    for alias, command in DEVICE_COMMAND_ALIASES.items():
        if stripped == alias or stripped.startswith(alias):
            return f"{command}{stripped[len(alias):]}"
    return stripped
