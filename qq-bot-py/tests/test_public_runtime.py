"""Public defaults and model portability without real external services."""
import asyncio
import ast
import copy
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from runtime_config import enabled_plugins, feature_enabled, validate_config
from plugins.model_transport import complete


def default_config():
    path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["allowed_groups"] = [100]
    config["database"]["password"] = "dummy-database"
    config["meme"]["minio"].update(access_key="dummy", secret_key="dummy-secret")
    config["ai"].update(api_url="https://example.test/v1/chat/completions",
                        api_key="dummy-key", model="gemini-compatible-vendor-name")
    return config


def test_basic_defaults_do_not_load_optional_integrations():
    config = default_config()
    validate_config(config)
    names = enabled_plugins(config)
    assert {"ai_chat", "admin_console", "duty_roster", "sign_in", "minigames"} <= set(names)
    assert not {"gsuid_bridge", "setu", "image_gen", "nai", "search", "web_search",
                "link_parser", "gbvsr_frame", "gemini_native"} & set(names)
    assert config["agent"]["provider_chain"] == ["primary"]
    assert config["agent"]["group"]["default_mode"] == "at"


def test_config_errors_identify_fields_without_secret_values():
    config = default_config()
    config["ai"]["model"] = ""
    with pytest.raises(ValueError, match="ai.model") as captured:
        validate_config(config)
    assert config["ai"]["api_key"] not in str(captured.value)


def test_core_requires_both_opt_in_switches():
    config = default_config()
    config["features"]["gsuid"] = True
    assert "gsuid_bridge" not in enabled_plugins(config)
    config["gsuid"]["enabled"] = True
    assert "gsuid_bridge" in enabled_plugins(config)


def test_chat_completions_keeps_images_and_uses_selected_model():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": " answer "},
            "finish_reason": "stop"}], "usage": {"total_tokens": 12}})
    messages = [{"role": "user", "content": [{"type": "image_url",
        "image_url": {"url": "data:image/png;base64,aW1hZ2U="}}]}]
    reply, usage = asyncio.run(complete(default_config()["ai"], messages,
        model="console-selected-model", transport=httpx.MockTransport(handler)))
    assert reply == "answer" and usage["total_tokens"] == 12
    assert requests == [{"model": "console-selected-model", "messages": messages, "stream": False}]


@pytest.mark.parametrize("status,body", [
    (401, {"error": "echoed-private-api-key"}),
    (200, {"choices": [{"message": {"content": ""}}]}),
    (200, {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]}),
    (200, {"wrong": "shape"}),
])
def test_provider_errors_are_actionable_and_never_echo_upstream_body(status, body):
    with pytest.raises(RuntimeError) as captured:
        asyncio.run(complete(default_config()["ai"], [],
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))))
    assert "echoed-private-api-key" not in str(captured.value)


def test_primary_protocol_does_not_guess_from_model_name(monkeypatch):
    """Execute the production dispatcher independently of matcher/DB imports."""
    source = Path(__file__).resolve().parents[1] / "plugins" / "ai_chat.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if getattr(node, "name", None) == "_call_primary"]
    from plugins import console_runtime
    import time
    from uuid import uuid4
    from plugins.agent_requests import REQUEST_CONTEXT
    monkeypatch.setattr(console_runtime, "global_value", lambda key, default: "ui-model")
    monkeypatch.setattr(console_runtime, "record", lambda *args, **kwargs: None)
    from plugins import model_transport
    callback = AsyncMock(return_value=("ok", {}))
    monkeypatch.setattr(model_transport, "complete", callback)
    namespace = {"__package__": "plugins", "REQUEST_CONTEXT": REQUEST_CONTEXT,
                 "PRIMARY_PROTOCOL": "chat_completions", "MODEL": "gemini-vendor",
                 "ai_cfg": default_config()["ai"], "PRIMARY_TIMEOUT": 90,
                 "time": time, "uuid4": uuid4}
    exec(compile(tree, str(source), "exec"), namespace)
    assert asyncio.run(namespace["_call_primary"]([])) == "ok"
    assert callback.await_args.kwargs["model"] == "ui-model"


def test_seeded_groups_default_to_at_and_use_primary_configured_model():
    from plugins.console_state import _defaults
    config = default_config()
    config["agent"]["active_groups"] = [100]
    values, seeds = _defaults(config)
    assert values["model"] == config["ai"]["model"]
    assert seeds[100]["mode"] == "at"


def test_disabled_tools_are_not_advertised_or_dispatchable(monkeypatch):
    from plugins import agent_tools
    monkeypatch.setattr(agent_tools, "_CONFIG", default_config())
    assert "send_setu" not in {tool["name"] for tool in agent_tools.tool_schemas("group")}
    assert "search_web" not in {tool["name"] for tool in agent_tools.tool_schemas("group")}
    assert agent_tools.tool_schemas("dev") == []
    with pytest.raises(PermissionError):
        asyncio.run(agent_tools.execute_tool("send_setu", {}, "group", {}))


def test_meme_images_read_inline_bytes_without_presigned_urls():
    source = Path(__file__).resolve().parents[1] / "plugins" / "meme_collector.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if getattr(node, "name", None) == "_presigned_url"]
    class Response:
        closed = False
        released = False
        def read(self): return b"image"
        def close(self): self.closed = True
        def release_conn(self): self.released = True
    response = Response()
    client = type("Minio", (), {"get_object": lambda *args: response})()
    namespace = {"minio_client": client, "BUCKET": "memes"}
    exec(compile(tree, str(source), "exec"), namespace)
    assert namespace["_presigned_url"]("meme.png") == "base64://aW1hZ2U="
    assert response.closed and response.released
