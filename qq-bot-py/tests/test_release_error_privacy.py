"""Upstream failures may echo requests: never log or forward their bodies."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from plugins import gsuid_bridge as bridge

SECRET = "private-key-and-private-prompt"


def fallback(response):
    source = Path(__file__).resolve().parents[1] / "plugins/ai_chat.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if getattr(node, "name", "") == "_call_fallback"]
    logs = []
    transport = httpx.MockTransport(lambda request: response)
    namespace = {
        "httpx": SimpleNamespace(AsyncClient=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs)),
        "logger": SimpleNamespace(info=logs.append), "_log_err": lambda tag, detail: logs.append(detail),
        "_strip_images": lambda value: value, "_with_provider_capability": lambda value, **_: value,
        "FALLBACK_URL": "https://model.invalid/v1/chat/completions", "FALLBACK_KEY": SECRET,
        "FALLBACK_MODEL": "test-model",
    }
    exec(compile(tree, str(source), "exec"), namespace)
    return namespace["_call_fallback"], logs


@pytest.mark.parametrize("response", [
    httpx.Response(502, text="upstream error " + SECRET),
    httpx.Response(200, text="broken JSON " + SECRET),
    httpx.Response(200, json={"error": {"message": SECRET}}),
    httpx.Response(200, json={"choices": SECRET}),
])
def test_fallback_errors_do_not_expose_upstream_body(response):
    call, logs = fallback(response)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(call([{"role": "user", "content": SECRET}]))
    assert SECRET not in str(caught.value)
    assert SECRET not in " ".join(logs)


def test_fallback_success_still_returns_content():
    call, _ = fallback(httpx.Response(200, json={"choices": [{"message": {"content": " valid reply "}}]}))
    assert asyncio.run(call([])) == "valid reply"


def test_nested_core_errors_are_not_forwarded_or_logged():
    async def run():
        bot = SimpleNamespace(self_id="123450001", send=AsyncMock(), send_private_msg=AsyncMock())
        content = [{"type": "node", "data": [{"type": "text", "data": "HTTP 401 " + SECRET}]}]
        data = {"bot_self_id": bot.self_id, "target_type": "direct", "target_id": "123456789", "content": content}
        with patch.object(bridge.logger, "warning") as warning, patch.object(bridge, "get_bots", return_value={"bot": bot}):
            await bridge._send_results(bot, SimpleNamespace(), [content])
            assert await bridge._send_unsolicited(data)
            assert warning.call_count == 2
            assert SECRET not in str(warning.call_args_list)
        bot.send.assert_not_awaited()
        bot.send_private_msg.assert_not_awaited()
    asyncio.run(run())


def test_core_connection_error_does_not_log_url_credentials():
    async def run():
        error = RuntimeError("wss://core.invalid/ws?token=" + SECRET)
        with patch.object(bridge.websockets, "connect", AsyncMock(side_effect=error)), \
                patch.object(bridge.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)), \
                patch.object(bridge.logger, "warning") as warning:
            with pytest.raises(asyncio.CancelledError):
                await bridge._ws_listener()
            assert SECRET not in str(warning.call_args_list)
            assert "RuntimeError" in str(warning.call_args_list)
    asyncio.run(run())


def test_core_send_error_does_not_log_upstream_content():
    async def run():
        bot = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError(SECRET)))
        with patch.object(bridge.logger, "warning") as warning:
            await bridge._send_results(bot, SimpleNamespace(), [[{"type": "text", "data": "ordinary reply"}]])
            assert SECRET not in str(warning.call_args_list)
            assert "RuntimeError" in str(warning.call_args_list)
    asyncio.run(run())
