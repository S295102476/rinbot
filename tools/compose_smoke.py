"""Linux CI smoke: fresh Compose deployment with fake model and fake OneBot.

Run from a CLEAN checkout: python tools/compose_smoke.py
Requires Docker Compose, PyYAML and websockets on the CI host. No QQ account,
real API token, or user configuration is used. Removes its containers/volumes.
"""
from __future__ import annotations

import asyncio
import base64
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18080"
BOT_ID, USER_ID, GROUP_ID = 10000, 10001, 10002
PASSWORD = "rinbot-ci-only-password"
REPLY = "RINBOT_CI_REPLY"
MODEL_REQUESTS: list[dict] = []


class Model(BaseHTTPRequestHandler):
    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        assert self.path == "/v1/chat/completions", self.path
        assert self.headers.get("Authorization") == "Bearer ci-only-model-key"
        assert request["model"] == "ci-model"
        assert request["messages"]
        MODEL_REQUESTS.append(request)
        content = json.dumps({"action": "reply", "source_message_id": 101, "replies": [REPLY],
                              "tool_calls": [], "affinity_updates": [], "quote": "none", "mentions": []})
        body = json.dumps({"id": "ci-request", "object": "chat.completion", "model": "ci-model",
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # Never emit prompts.


class Console:
    def __init__(self):
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))
        self.csrf = ""

    def request(self, path, payload=None, method=None):
        headers = {"Origin": BASE}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        body = json.dumps(payload).encode() if payload is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = self.client.open(Request(BASE + path, body, headers, method=method), timeout=10)
        data = json.loads(response.read())
        assert "ci-only-model-key" not in json.dumps(data), "API leaked model key"
        return data

    def login(self):
        data = self.request("/api/admin/auth/login", {"username": "admin", "password": PASSWORD})
        self.csrf = data["csrf_token"]


def wait_ready(timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(BASE + "/admin/", timeout=3) as response:
                if response.status == 200 and b"<html" in response.read(4096).lower():
                    return
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(2)
    raise AssertionError("Bot never served the built console")


def message(text: str, message_id: int, *, at=False):
    segments = ([{"type": "at", "data": {"qq": str(BOT_ID)}}] if at else [])
    segments.append({"type": "text", "data": {"text": text}})
    # OneBot adapters set `event.to_me` from the leading @ segment and may
    # remove that segment from `event.message`. They preserve the wire-level
    # raw_message, which RinBot's scope gate also reads as a fallback. Keep the
    # CQ marker here so this fixture models a real adapter event instead of
    # weakening the production only-@ gate.
    raw_message = (f"[CQ:at,qq={BOT_ID}]" if at else "") + text
    return {"time": int(time.time()), "self_id": BOT_ID, "post_type": "message", "message_type": "group",
            "sub_type": "normal", "message_id": message_id, "group_id": GROUP_ID, "user_id": USER_ID,
            "message": segments, "raw_message": raw_message, "font": 0,
            "sender": {"user_id": USER_ID, "nickname": "CI User", "card": "", "sex": "unknown",
                       "age": 18, "area": "", "level": "1", "role": "owner", "title": ""}}


async def onebot(token: str, console: Console):
    from websockets.asyncio.client import connect
    uri = "ws://127.0.0.1:18080/onebot/v11/ws"
    headers = {"Authorization": "Bearer " + token, "X-Self-ID": str(BOT_ID), "X-Client-Role": "Universal"}
    sent: list[dict] = []
    async with connect(uri, additional_headers=headers, max_size=16 * 1024 * 1024) as ws:
        async def consume():
            async for raw in ws:
                call = json.loads(raw)
                action, params = call.get("action", ""), call.get("params", {})
                member = {"user_id": USER_ID, "nickname": "CI User", "card": "", "role": "owner"}
                if action == "get_login_info":
                    data = {"user_id": BOT_ID, "nickname": "RinBot CI"}
                elif action == "get_group_list":
                    data = [{"group_id": GROUP_ID, "group_name": "CI Group", "member_count": 2, "max_member_count": 20}]
                elif action in {"get_group_member_info", "get_stranger_info"}:
                    data = {**member, "user_id": params.get("user_id", USER_ID)}
                elif action == "get_group_member_list":
                    data = [member, {**member, "user_id": BOT_ID, "nickname": "RinBot CI", "role": "admin"}]
                elif action == "get_group_info":
                    data = {"group_id": GROUP_ID, "group_name": "CI Group", "member_count": 2, "max_member_count": 20}
                elif action.startswith("send_"):
                    sent.append(params)
                    data = {"message_id": 1000 + len(sent)}
                else:
                    data = {}
                await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": call.get("echo")}))

        reader = asyncio.create_task(consume())
        try:
            await ws.send(json.dumps({"time": int(time.time()), "self_id": BOT_ID, "post_type": "meta_event",
                                      "meta_event_type": "lifecycle", "sub_type": "connect"}))
            await asyncio.sleep(2)
            overview = await asyncio.to_thread(console.request, "/api/admin/overview")
            assert overview["status"]["bot_online"], "Console did not recognize OneBot"
            groups = await asyncio.to_thread(console.request, "/api/admin/groups")
            assert groups["items"] and groups["items"][0]["mode"] == "at", "Fresh groups must be @-only"

            # Non-mentioned chatter cannot consume the model by default.
            await ws.send(json.dumps(message("This is ordinary CI chatter", 100)))
            await asyncio.sleep(5)
            assert not MODEL_REQUESTS, "Fresh group called the model without an @"
            await ws.send(json.dumps(message(" Please reply to this CI hello", 101, at=True)))
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and not any(REPLY in json.dumps(p) for p in sent):
                if reader.done():
                    reader.result()
                await asyncio.sleep(1)
            assert MODEL_REQUESTS, "Real configured HTTP model transport was never called"
            assert any(REPLY in json.dumps(p) for p in sent), "Model response was not sent through OneBot"

            # Duty roster uses local avatars and no external avatar/background APIs.
            sent.clear()
            await ws.send(json.dumps(message("#值班表", 102)))
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline and not any("base64://" in json.dumps(p) for p in sent):
                if reader.done():
                    reader.result()
                await asyncio.sleep(1)
            images = []
            for payload in sent:
                for segment in payload.get("message", []) if isinstance(payload.get("message"), list) else []:
                    if segment.get("type") == "image":
                        images.append(segment["data"].get("file", ""))
            assert any(value.startswith("base64://") and len(base64.b64decode(value[9:])) > 1000 for value in images), "Roster image was not sent as bytes"
            assert all("minio:9000" not in json.dumps(p) for p in sent), "Image exposed an internal MinIO URL"
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


def seed_persistence(console: Console) -> dict:
    """Use the real editor APIs after the @-only and image delivery assertions."""
    group = next(row for row in console.request("/api/admin/groups")["items"] if row["group_id"] == GROUP_ID)
    group_patch = {"mode": "auto", "daily_reply_limit": 123, "hourly_reply_soft_limit": 17}
    saved_group = console.request(f"/api/admin/groups/{GROUP_ID}",
                                  {**group_patch, "version": group["version"]}, method="PATCH")
    assert all(saved_group[key] == value for key, value in group_patch.items())

    personas = console.request("/api/admin/personas")
    active = next(row for row in personas["items"] if row["persona_id"] == personas["active_id"])
    document_meta = next(row for row in active["documents"] if not row["shared"] and row["name"] == "prompt.md")
    document_path = "/api/admin/personas/documents/" + document_meta["id"]
    original = console.request(document_path)
    content = original["content"] + "\n\n<!-- RinBot CI: persona editor persistence -->\n"
    saved_document = console.request(document_path,
        {"content": content, "version": original["version"], "reason": "CI restart persistence"}, method="PUT")
    assert saved_document["ok"] and saved_document["content"] == content

    memory_payload = {"group_id": GROUP_ID, "user_id": USER_ID, "fact": "CI user prefers durable configuration.",
                      "category": "preference", "importance": 2, "confidence": 1.0,
                      "reason": "CI business data persistence"}
    memory = console.request("/api/admin/memory/facts?scope=group", memory_payload)["item"]
    assert memory["protected"] and memory["fact"] == memory_payload["fact"]
    return {"group": group_patch, "group_version": saved_group["version"],
            "document_path": document_path, "document_content": content,
            "document_version": saved_document["version"], "original_version": original["version"],
            "memory_id": memory["id"], "memory_fact": memory_payload["fact"]}


def verify_persistence(console: Console, expected: dict) -> None:
    group = next(row for row in console.request("/api/admin/groups")["items"] if row["group_id"] == GROUP_ID)
    assert all(group[key] == value for key, value in expected["group"].items()), "Group settings lost after restart"
    assert group["version"] == expected["group_version"], "Group version changed unexpectedly"
    document = console.request(expected["document_path"])
    assert document["content"] == expected["document_content"], "Persona edit lost after restart"
    assert document["version"] == expected["document_version"]
    revisions = console.request(expected["document_path"] + "/revisions")["items"]
    assert {expected["original_version"], expected["document_version"]} <= {row["version"] for row in revisions}, "Persona revision history lost"
    memories = console.request(f"/api/admin/memory/facts?scope=group&group_id={GROUP_ID}&user_id={USER_ID}")["items"]
    memory = next(row for row in memories if row["id"] == expected["memory_id"])
    assert memory["fact"] == expected["memory_fact"] and memory["protected"], "Business memory/protection lost"


def main():
    if any((ROOT / path).exists() for path in (".env", "runtime")):
        raise SystemExit("Run smoke tests in a clean clone; existing .env/runtime will never be replaced")
    os.chdir(ROOT)
    env = {**os.environ, "RINBOT_INIT_API_URL": "http://host.docker.internal:18090/v1/chat/completions",
           "RINBOT_INIT_API_KEY": "ci-only-model-key", "RINBOT_INIT_MODEL": "ci-model",
           "RINBOT_INIT_ADMIN_QQ": str(USER_ID), "RINBOT_INIT_GROUPS": str(GROUP_ID),
           "RINBOT_INIT_CONSOLE_PASSWORD": PASSWORD}
    server = ThreadingHTTPServer(("0.0.0.0", 18090), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="rinbot-compose-smoke-") as temporary:
        override = Path(temporary) / "compose.override.yaml"
        override.write_text("services:\n  bot:\n    extra_hosts:\n      - 'host.docker.internal:host-gateway'\n", encoding="utf-8")
        base = ["docker", "compose", "--project-name", "rinbot-smoke", "-f", str(ROOT / "compose.yaml"), "-f", str(override)]

        def compose(*args, capture=False, check=True):
            result = subprocess.run([*base, *args], env=env, check=check, text=True,
                                    stdout=subprocess.PIPE if capture else None)
            return result.stdout if capture else result

        try:
            compose("config", "--quiet")  # Must work before .env/runtime exist.
            flags = [item for key in env if key.startswith("RINBOT_INIT_") for item in ("-e", key)]
            compose("run", "--rm", "-T", *flags, "init", "--root", "/workspace", "--non-interactive")
            compose_env = ROOT / ".env"
            compose_env.write_text(compose_env.read_text().replace("RINBOT_PORT='8080'", "RINBOT_PORT='18080'"), encoding="utf-8")
            bot_env = ROOT / "runtime/bot.env"
            bot_env.write_text(bot_env.read_text().replace("http://127.0.0.1:8080", "http://127.0.0.1:18080").replace("http://localhost:8080", "http://localhost:18080"), encoding="utf-8")
            # Up-front cleanup is intentionally absent: this test only owns its newly created project.
            compose("up", "--build", "--detach", "--wait", "--wait-timeout", "300")
            wait_ready()
            try:
                urlopen(BASE + "/api/admin/auth/me", timeout=5)
            except HTTPError as exc:
                assert exc.code == 401
            else:
                raise AssertionError("Anonymous console access did not fail closed")
            console = Console()
            console.login()
            settings = console.request("/api/admin/settings")
            assert settings["model"] == "ci-model"
            console.request("/api/admin/settings", {"daily_reply_limit": 199, "version": settings["version"]}, method="PATCH")
            token_line = next(line for line in bot_env.read_text().splitlines() if line.startswith("ONEBOT_ACCESS_TOKEN="))
            token = token_line.split("=", 1)[1].strip("'")
            asyncio.run(onebot(token, console))
            expected_persistence = seed_persistence(console)
            compose("exec", "-T", "bot", "python", "-c", "from pathlib import Path; Path('data/.ci-persistence').write_text('persisted'); Path('persona/.ci-persistence').write_text('persisted')")
            compose("restart", "bot")
            wait_ready()
            # Redis-backed login must survive, as must MySQL settings and named volumes.
            settings = console.request("/api/admin/settings")
            assert settings["daily_reply_limit"] == 199
            verify_persistence(console, expected_persistence)
            compose("exec", "-T", "bot", "python", "-c", "from pathlib import Path; assert Path('data/.ci-persistence').read_text() == 'persisted'; assert Path('persona/.ci-persistence').read_text() == 'persisted'")
            compose("exec", "-T", "bot", "python", "tools/container_health.py")
            print("PASS: fresh four-service deployment, auth, model/@ reply, roster image, group/persona/memory restart persistence")
        except BaseException:
            # Logs contain only synthetic credentials and inputs in this isolated test.
            compose("logs", "--no-color", "--tail", "200", check=False)
            raise
        finally:
            compose("down", "--volumes", "--remove-orphans", check=False)
            server.shutdown()


if __name__ == "__main__":
    main()
