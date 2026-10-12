"""Add an authenticated public listener to an existing Mihomo instance."""

import argparse
import copy
import getpass
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import yaml


def progress(message):
    print(message, flush=True)


class Controller:
    def __init__(self, url, secret):
        self.url = url.rstrip("/")
        self.client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.headers = {"Content-Type": "application/json"}
        if secret:
            self.headers["Authorization"] = "Bearer " + str(secret)

    def call(self, endpoint, data=None):
        request = urllib.request.Request(
            self.url + endpoint,
            data=None if data is None else json.dumps(data).encode(),
            headers=self.headers,
            method="GET" if data is None else "PUT",
        )
        with self.client.open(request, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}


def validate_credential(value, field):
    if not isinstance(value, str) or not 1 <= len(value.encode()) <= 255:
        raise ValueError(f"{field} must contain 1-255 UTF-8 bytes")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")
    if field == "Username" and ":" in value:
        raise ValueError("Username must not contain ':' (HTTP authentication)")


def validate_users(users):
    if not isinstance(users, list) or not users:
        raise ValueError("At least one authenticated user is required")
    names = set()
    for user in users:
        if not isinstance(user, dict):
            raise ValueError("Each user must contain username and password")
        validate_credential(user.get("username"), "Username")
        validate_credential(user.get("password"), "Password")
        if user["username"] in names:
            raise ValueError("Duplicate usernames are not allowed")
        names.add(user["username"])


def prompt_users(names):
    if not sys.stdin.isatty():
        raise ValueError("Run --set-user in an interactive SSH terminal; do not pipe passwords")
    if len(set(names)) != len(names):
        raise ValueError("Specify each --set-user only once")
    for name in names:
        validate_credential(name, "Username")
    users = []
    for name in names:
        password = getpass.getpass(f"Password for {name}: ")
        validate_credential(password, "Password")
        if len(password) < 12:
            raise ValueError("New passwords must contain at least 12 characters")
        if getpass.getpass(f"Confirm password for {name}: ") != password:
            raise ValueError("Passwords do not match; configuration unchanged")
        users.append({"username": name, "password": password})
    return users


def listener_config(config, port, username, outbound, user_updates=None):
    if user_updates is not None:
        validate_users(user_updates)
    updated = copy.deepcopy(config)
    if not any(p.get("name") == outbound for p in updated.get("proxies", [])):
        raise ValueError(f"Outbound {outbound} was not found; configuration unchanged")
    name = f"share-auth-{port}"
    listeners = updated.setdefault("listeners", [])
    existing = next((item for item in listeners if item.get("name") == name), None)
    if existing:
        if (existing.get("type") != "mixed" or int(existing.get("port", 0)) != port
                or existing.get("listen") != "0.0.0.0" or existing.get("proxy") != outbound
                or existing.get("udp") is not False):
            raise ValueError("Existing share listener has different settings; stopped")
        users = existing.get("users") or []
        validate_users(users)
        before = copy.deepcopy(users)
        for new_user in user_updates or []:
            matched = next((user for user in users if user["username"] == new_user["username"]), None)
            if matched is None:
                users.append(copy.deepcopy(new_user))
            else:
                matched["password"] = new_user["password"]
        return updated, users, users != before
    if any(str(config.get(key)) == str(port) for key in
           ("port", "socks-port", "mixed-port", "redir-port", "tproxy-port")):
        raise ValueError("Port belongs to another main listener")
    if any(str(item.get("port")) == str(port) for item in listeners):
        raise ValueError("Port belongs to another listener")
    users = copy.deepcopy(user_updates) if user_updates is not None else [
        {"username": username, "password": secrets.token_urlsafe(24)}]
    validate_users(users)
    listeners.append({
        "name": name, "type": "mixed", "listen": "0.0.0.0", "port": port,
        "proxy": outbound, "udp": False,
        "users": users,
    })
    return updated, users, True


def private_file(path, text):
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def receive(sock, length):
    result = b""
    while len(result) < length:
        part = sock.recv(length - len(result))
        if not part:
            raise RuntimeError("Proxy closed the authentication handshake")
        result += part
    return result


def verify_auth(port, username, password):
    # Verify both required authentication and the saved credentials without dialing upstream.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"\x05\x01\x00")
        # Mihomo requests password auth even when only no-auth was offered.
        if receive(sock, 2) not in (b"\x05\xff", b"\x05\x02"):
            raise RuntimeError("Listener did not reject unauthenticated SOCKS5 access")
    user, secret = username.encode(), password.encode()
    if not 1 <= len(user) <= 255 or not 1 <= len(secret) <= 255:
        raise ValueError("SOCKS5 username/password must contain 1-255 UTF-8 bytes")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"\x05\x01\x02")
        if receive(sock, 2) != b"\x05\x02":
            raise RuntimeError("Listener did not select username/password authentication")
        invalid = secrets.token_urlsafe(24).encode()
        while invalid == secret:
            invalid = secrets.token_urlsafe(24).encode()
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(invalid)]) + invalid)
        if receive(sock, 2) != b"\x01\x01":
            raise RuntimeError("Listener did not reject an incorrect password")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"\x05\x01\x02")
        if receive(sock, 2) != b"\x05\x02":
            raise RuntimeError("Listener did not select username/password authentication")
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
        if receive(sock, 2) != b"\x01\x00":
            raise RuntimeError("Share credentials were rejected")


def reload_config(controller, path, selected):
    progress("Reloading Mihomo configuration...")
    controller.call("/configs?force=true", {"path": str(path)})
    progress(f"Restoring {len(selected)} existing selector choices...")
    for group, choice in selected.items():
        controller.call("/proxies/" + urllib.parse.quote(group, safe=""), {"name": choice})


def verify_users(port, users):
    for user in users:
        verify_auth(port, user["username"], user["password"])


def apply_config(path, updated, controller, selected, service, port, users, info,
                 address="proxy.example.com", expected_source=None):
    source = path.read_text(encoding="utf-8")
    if expected_source is not None and source != expected_source:
        raise RuntimeError("Configuration changed while entering passwords; rerun the command")
    original = path.stat()
    text = yaml.safe_dump(updated, allow_unicode=True, sort_keys=False)
    candidate = private_file(path, text)
    backup = None
    changed = False
    try:
        progress("Validating configuration with the running Mihomo executable...")
        pid = subprocess.check_output(
            ["systemctl", "show", service, "-p", "MainPID", "--value"],
            text=True, timeout=10,
        ).strip()
        if not pid.isdigit() or int(pid) == 0:
            raise RuntimeError("Mihomo service is not running; configuration unchanged")
        subprocess.run(
            [f"/proc/{pid}/exe", "-t", "-d", str(path.parent), "-f", str(candidate)],
            check=True, timeout=45,
        )
        backup = path.with_name(f"config.yaml.before-share-auth.{time.time_ns()}")
        if path.read_text(encoding="utf-8") != source:
            raise RuntimeError("Configuration changed during validation; rerun the command")
        backup_file = private_file(backup, source)
        backup_file.replace(backup)
        progress(f"Backup: {backup}")
        candidate.chmod(stat.S_IMODE(original.st_mode))
        if hasattr(os, "chown"):
            os.chown(candidate, original.st_uid, original.st_gid)
        candidate.replace(path)
        changed = True
        reload_config(controller, path, selected)
        progress("Checking listener and username/password authentication...")
        verify_users(port, users)
        info_file = private_file(info, credential_text(port, users, address))
        info_file.replace(info)
    except BaseException:
        if changed:
            progress("Setup failed; restoring the original configuration...")
            restore = private_file(path, backup.read_text(encoding="utf-8"))
            restore.chmod(stat.S_IMODE(original.st_mode))
            if hasattr(os, "chown"):
                os.chown(restore, original.st_uid, original.st_gid)
            restore.replace(path)
            try:
                reload_config(controller, path, selected)
                progress("Original configuration restored.")
            except Exception as error:
                progress(f"Rollback reload failed ({type(error).__name__}); backup: {backup}")
        raise
    finally:
        candidate.unlink(missing_ok=True)


def credential_text(port, users, address="proxy.example.com"):
    return (
        "Protocol: HTTP / SOCKS5\n"
        f"Address: {address}\nPort: {port}\n"
        + "".join(f"\nUsername: {user['username']}\nPassword: {user['password']}\n" for user in users)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/opt/mihomo/config/config.yaml"))
    parser.add_argument("--api-url", default="http://127.0.0.1:9090")
    parser.add_argument("--service", default="mihomo")
    parser.add_argument("--port", type=int, default=17895)
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--username", default="proxyuser", help="Initial username with a random password")
    credentials.add_argument("--set-user", action="append", default=[], metavar="NAME",
                             help="Add/update a user with a hidden password prompt; repeat for multiple users")
    parser.add_argument("--address", default="proxy.example.com", help="Public hostname or IP for the credential sheet")
    parser.add_argument("--outbound", required=True, help="Existing outbound proxy name from your Mihomo configuration")
    args = parser.parse_args()
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        parser.error("Run this command as root")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    validate_credential(args.username, "Username")
    if not args.address or any(char.isspace() for char in args.address) or any(
            char in args.address for char in "/:@?#"):
        parser.error("Use a hostname or IPv4 address without https://, a port or a path")
    path = args.config.resolve(strict=True)
    info = Path(f"/root/proxy-share-{args.port}.txt")
    progress("Reading Mihomo configuration...")
    source = path.read_text(encoding="utf-8")
    config = yaml.safe_load(source)
    if not isinstance(config, dict):
        raise ValueError("Mihomo configuration must be a YAML mapping")
    updated, users, needs_apply = listener_config(config, args.port, args.username, args.outbound)
    if needs_apply:  # No existing share listener: check port before asking for passwords.
        if info.exists():
            raise ValueError(f"Credentials file already exists: {info}; stopped without replacing it")
        with socket.socket() as sock:
            sock.bind(("0.0.0.0", args.port))
    if args.set_user:
        user_updates = prompt_users(args.set_user)
        updated, users, needs_apply = listener_config(
            config, args.port, args.username, args.outbound, user_updates)
    if needs_apply:
        progress("Reading current selector choices from the local controller...")
        controller = Controller(args.api_url, config.get("secret"))
        proxies = controller.call("/proxies")["proxies"]
        selected = {name: item["now"] for name, item in proxies.items()
                    if item.get("type") == "Selector" and item.get("now")}
        apply_config(path, updated, controller, selected, args.service,
                     args.port, users, info, args.address, source)
    else:
        progress("Existing share listener found; keeping its users and checking authentication...")
        verify_users(args.port, users)
        temporary = private_file(info, credential_text(args.port, users, args.address))
        temporary.replace(info)
    progress("Share proxy is ready. Still allow the TCP port in your cloud firewall.")
    progress(f"Address: {args.address} | TCP port: {args.port} | Users: {len(users)}")
    progress("DNS must point directly to this server; no https:// prefix or ordinary CDN proxy.")
    progress(f"Credentials saved with mode 600: {info}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(f"ERROR ({type(error).__name__}): {error}") from None
