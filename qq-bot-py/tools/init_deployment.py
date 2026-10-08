"""Create a fresh private deployment without importing or starting any plugins.

Interactive: docker compose run --rm init
CI: --non-interactive reads RINBOT_INIT_* environment variables, never argv secrets.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlsplit

import yaml

APP_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Answers:
    api_url: str
    api_key: str
    model: str
    admin: int
    groups: tuple[int, ...]
    console_password: str


def parse_ids(value: str) -> tuple[int, ...]:
    parts = re.split(r"[,，\s]+", value.strip())
    if not parts or any(not part.isdecimal() or not 1 <= int(part) < 2**63 for part in parts):
        raise ValueError("QQ/group IDs must be positive numbers separated by commas or spaces")
    return tuple(dict.fromkeys(int(part) for part in parts))


def validate_answers(answers: Answers) -> None:
    parsed = urlsplit(answers.api_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment
            or any(char.isspace() for char in answers.api_url)):
        raise ValueError("Provide a complete HTTP(S) API endpoint without embedded credentials")
    # Force validation of malformed ports without echoing the URL or credential.
    try:
        parsed.port
    except ValueError:
        raise ValueError("The API endpoint port is invalid") from None
    for label, value in (("API key", answers.api_key), ("Model", answers.model)):
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError(f"{label} must be nonempty and contain no control characters")
    if len(answers.model) > 200:
        raise ValueError("Model name is too long")
    if not 1 <= answers.admin < 2**63 or not answers.groups or any(not 1 <= g < 2**63 for g in answers.groups):
        raise ValueError("Provide an administrator QQ and at least one allowed group")
    if not 12 <= len(answers.console_password) <= 1024:
        raise ValueError("Console password must contain 12 to 1024 characters")


def encode_password(password: str) -> str:
    """Wire format of plugins.console_auth.hash_password, without FastAPI import."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii")
    return f"scrypt$v1$16384$8$1${encode(salt)}${encode(digest)}"


def dotenv(values: dict[str, str]) -> str:
    # Single quotes prevent Compose/python-dotenv from interpolating $ in hashes.
    def quote(value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("Environment values must fit on one line")
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return "\n".join(f"{key}={quote(value)}" for key, value in values.items()) + "\n"


def ensure_fresh(root: Path) -> None:
    if not root.is_dir():
        raise ValueError("Deployment root must be an existing directory")
    paths = [root / ".env", root / "runtime", root / "qq-bot-py/config.yaml", root / "qq-bot-py/.env"]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise FileExistsError("Existing configuration/runtime detected; initialization never overwrites it. Use a fresh clone or restore your backup.")


def initialize(root: Path, answers: Answers, *, template: Path = APP_ROOT / "config.example.yaml") -> None:
    """Write only newly created files, remove this invocation's files on failure."""
    root = root.resolve()
    ensure_fresh(root)
    validate_answers(answers)
    config = yaml.safe_load(template.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Public configuration template is invalid")
    token = lambda: secrets.token_urlsafe(32)
    mysql_password, mysql_root_password, redis_password = token(), token(), token()
    minio_user, minio_password, onebot_token = "rinbot-" + secrets.token_hex(8), token(), token()
    config["database"]["password"] = mysql_password
    config["redis"]["password"] = redis_password
    config["ai"].update(api_url=answers.api_url, api_key=answers.api_key, model=answers.model)
    config["meme"]["minio"].update(access_key=minio_user, secret_key=minio_password)
    groups = list(answers.groups)
    config["allowed_groups"] = groups
    config["agent"]["active_groups"] = groups
    config["ai"]["group_chat"]["enabled_groups"] = groups
    config["minigames"]["allowed_groups"] = groups
    config["deer"]["allowed_groups"] = groups
    config["agent"]["dev"]["admin_users"] = [answers.admin]
    config["setu"]["admin_users"] = [answers.admin]
    config["wiki"]["admin_users"] = [answers.admin]
    config["agent"]["group"]["owner_user_id"] = answers.admin

    compose_values = {
        "RINBOT_BIND_HOST": "127.0.0.1", "RINBOT_PORT": "8080",
        "MYSQL_PASSWORD": mysql_password, "MYSQL_ROOT_PASSWORD": mysql_root_password,
        "REDIS_PASSWORD": redis_password, "MINIO_ROOT_USER": minio_user,
        "MINIO_ROOT_PASSWORD": minio_password,
    }
    bot_values = {
        "HOST": "0.0.0.0", "PORT": "8080", "ENVIRONMENT": "prod", "LOG_LEVEL": "INFO",
        "SUPERUSERS": json.dumps([str(answers.admin)]), "COMMAND_START": json.dumps([""]),
        "ONEBOT_ACCESS_TOKEN": onebot_token,
        "AGENT_CONSOLE_USERNAME": "admin", "AGENT_CONSOLE_PASSWORD_HASH": encode_password(answers.console_password),
        "AGENT_CONSOLE_SESSION_BACKEND": "redis", "AGENT_CONSOLE_RUNTIME_ENABLED": "true",
        "AGENT_CONSOLE_ORIGINS": "http://127.0.0.1:8080,http://localhost:8080",
    }
    runtime = root / "runtime"
    created: list[Path] = []
    runtime.mkdir(mode=0o700)  # also atomically prevents two initializers racing
    try:
        files = (
            (runtime / "config.yaml", yaml.safe_dump(config, allow_unicode=True, sort_keys=False)),
            (runtime / "bot.env", dotenv(bot_values)),
            (root / ".env", dotenv(compose_values)),
        )
        for path, content in files:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        runtime.rmdir()
        raise


def collect_answers(non_interactive: bool = False) -> Answers:
    if non_interactive:
        names = ("API_URL", "API_KEY", "MODEL", "ADMIN_QQ", "GROUPS", "CONSOLE_PASSWORD")
        values = [os.getenv("RINBOT_INIT_" + name, "") for name in names]
        if not all(values):
            raise ValueError("Non-interactive init requires RINBOT_INIT_" + ", RINBOT_INIT_".join(names))
        api_url, api_key, model, admin, groups, password = values
    else:
        print("RinBot fresh deployment / 新部署初始化（不会启动或连接 QQ）")
        api_url = input("Chat Completions 完整接口地址: ").strip()
        api_key = getpass("模型 API Key: ").strip()
        model = input("模型名称: ").strip()
        admin = input("管理员 QQ: ").strip()
        groups = input("允许使用的群号（多个用逗号分隔）: ").strip()
        password = getpass("控制台密码（至少 12 位）: ")
        if password != getpass("再次输入控制台密码: "):
            raise ValueError("Passwords do not match")
    admins = parse_ids(admin)
    if len(admins) != 1:
        raise ValueError("Provide exactly one administrator QQ")
    return Answers(api_url.strip(), api_key.strip(), model.strip(), admins[0], parse_ids(groups), password)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=APP_ROOT.parent)
    parser.add_argument("--match-owner", action="store_true", help="In a Linux container create files as the bind mount owner")
    parser.add_argument("--non-interactive", action="store_true", help="Read RINBOT_INIT_* env variables (for CI)")
    args = parser.parse_args()
    try:
        # Docker defaults to root. Match the clone's owner so 0600 secrets remain
        # editable by the person who ran Compose on Linux (Docker Desktop keeps
        # its own Windows permission mapping). The server image remains separate.
        if args.match_owner and hasattr(os, "geteuid") and os.geteuid() == 0:
            owner = args.root.resolve().stat()
            if owner.st_uid != 0:
                os.setgroups([])
                os.setgid(owner.st_gid)
                os.setuid(owner.st_uid)
        ensure_fresh(args.root.resolve())
        initialize(args.root, collect_answers(args.non_interactive))
    except (ValueError, FileExistsError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    print("已生成 .env、runtime/config.yaml 和 runtime/bot.env；密码和密钥未输出。")
    print("下一步：docker compose up -d --build")
    print("控制台：http://127.0.0.1:8080/admin/  用户名：admin")
    print("将 runtime/bot.env 中 ONEBOT_ACCESS_TOKEN 配置到你自己的 OneBot v11 客户端。")
    print("同机反向 WebSocket：ws://127.0.0.1:8080/onebot/v11/ws")


if __name__ == "__main__":
    main()
