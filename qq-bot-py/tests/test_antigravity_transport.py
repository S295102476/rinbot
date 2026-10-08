import ast
import asyncio
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4

import httpx

from plugins.agent_requests import REQUEST_CONTEXT, request_context


def load_transport(handler):
    # Load the real transport without NoneBot matchers, MinIO or database startup.
    source = Path(__file__).resolve().parents[1] / "plugins" / "ai_chat.py"
    names = {
        "_strip_images", "_build_antigravity_payload",
        "_is_antigravity_search_tool_error", "_call_antigravity_openai",
        "_call_antigravity", "_with_provider_capability",
    }
    module = ast.parse(source.read_text(encoding="utf-8"))
    module.body = [node for node in module.body if getattr(node, "name", "") in names]
    logs = []
    transport = httpx.MockTransport(handler)
    namespace = {
        "asyncio": asyncio, "json": json, "re": re, "time": time, "uuid4": uuid4,
        "REQUEST_CONTEXT": REQUEST_CONTEXT,
        "httpx": SimpleNamespace(
            AsyncHTTPTransport=lambda **kwargs: transport,
            AsyncClient=httpx.AsyncClient,
        ),
        "logger": SimpleNamespace(info=logs.append, warning=logs.append),
        "_with_provider_capability": lambda messages, **kwargs: messages,
        "ANTIGRAVITY_MODEL": "test-model",
        "ANTIGRAVITY_KEY": "never-log-this-key",
        "ANTIGRAVITY_URL": "https://example.test/v1/chat/completions",
        "ANTIGRAVITY_TIMEOUT": 90,
        "ANTIGRAVITY_ENABLE_SEARCH": True,
        "ANTIGRAVITY_PROXY": None,
        "ANTIGRAVITY_PROTOCOL": "openai",
        "_antigravity_available": lambda: True,
        "_SEARCH_AVAILABLE_RULE": "search available",
        "_SEARCH_UNAVAILABLE_RULE": "search unavailable",
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["_call_antigravity_openai"], logs


def success_response():
    return httpx.Response(200, headers={"x-request-id": "upstream-123"}, json={
        "id": "chatcmpl-456",
        "choices": [{"message": {"content": " answer "}, "finish_reason": "stop"}],
    })


class AntigravityTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_timeout_metadata_and_success_are_correlated(self):
        requests = []

        def handler(request):
            requests.append(request)
            return success_response()

        call, logs = load_transport(handler)
        with request_context("summary", 123, 180) as request_id:
            result = await call([{"role": "user", "content": "secret-prompt"}], enable_search=False)
        self.assertEqual(result, "answer")
        self.assertEqual(requests[0].extensions["timeout"]["read"], 180)
        self.assertEqual(requests[0].headers["x-request-id"], request_id)
        self.assertIsNone(REQUEST_CONTEXT.get())
        self.assertIn("request_id=" + request_id, logs[0])
        self.assertIn("group=123 purpose=summary", logs[0])
        self.assertIn("images=0", logs[0])
        self.assertIn("status=success", logs[-1])
        self.assertIn("http_status=200 upstream_id=upstream-123 response_id=chatcmpl-456", logs[-1])
        self.assertNotIn("secret-prompt", " ".join(logs))
        self.assertNotIn("never-log-this-key", " ".join(logs))

    async def test_explicit_timeout_wins_and_unscoped_requests_keep_default(self):
        timeouts = []

        def handler(request):
            timeouts.append(request.extensions["timeout"]["read"])
            return success_response()

        call, logs = load_transport(handler)
        with request_context("summary", 123, 180):
            await call([], timeout=210)
        await call([])
        self.assertEqual(timeouts, [210, 90])
        self.assertIn("purpose=chat", logs[-1])

    async def test_search_rejection_retries_once_with_same_request_id(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(400, json={"error": "googleSearch unsupported"})
            return success_response()

        call, logs = load_transport(handler)
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
        ]}]
        await call(messages, preserve_images=True)
        self.assertEqual(len(requests), 2)
        self.assertIn("tools", json.loads(requests[0].content))
        self.assertNotIn("tools", json.loads(requests[1].content))
        self.assertEqual(requests[0].headers["x-request-id"], requests[1].headers["x-request-id"])
        self.assertTrue(any("status=retry_without_search" in log for log in logs))
        self.assertIn("images=1", logs[0])
        self.assertNotIn("aGVsbG8=", " ".join(logs))

    async def test_http_timeout_is_logged_and_propagated(self):
        def handler(request):
            raise httpx.ReadTimeout("secret-provider-detail", request=request)

        call, logs = load_transport(handler)
        with self.assertRaises(httpx.ReadTimeout):
            await call([])
        self.assertIn("status=failed", logs[-1])
        self.assertIn("error=ReadTimeout", logs[-1])
        self.assertIn("http_status=-", logs[-1])
        self.assertNotIn("secret-provider-detail", " ".join(logs))

    async def test_cancellation_is_logged_without_swallowing(self):
        started = asyncio.Event()

        async def handler(request):
            started.set()
            await asyncio.Event().wait()

        call, logs = load_transport(handler)
        task = asyncio.create_task(call([]))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn("status=cancelled", logs[-1])
        self.assertIn("error=CancelledError", logs[-1])

    async def test_error_response_never_logs_or_raises_provider_body(self):
        def handler(request):
            return httpx.Response(503, json={"error": "secret-prompt-in-provider-error"})

        call, logs = load_transport(handler)
        with self.assertRaises(RuntimeError) as caught:
            await call([])
        self.assertNotIn("secret-prompt", str(caught.exception))
        self.assertNotIn("secret-prompt", " ".join(logs))
        self.assertIn("http_status=503", logs[-1])

    async def test_incomplete_output_remains_rejected(self):
        def handler(request):
            return httpx.Response(200, json={
                "id": "chatcmpl-blocked",
                "choices": [{"message": {"content": "partial-secret"}, "finish_reason": "length"}],
            })

        call, logs = load_transport(handler)
        with self.assertRaises(RuntimeError):
            await call([])
        self.assertIn("status=failed", logs[-1])
        self.assertIn("response_id=chatcmpl-blocked", logs[-1])
        self.assertNotIn("partial-secret", " ".join(logs))

    async def test_concurrent_scopes_do_not_mix_groups(self):
        contexts = []

        async def work(group_id):
            with request_context("group_decision", group_id, 90) as request_id:
                await asyncio.sleep(0)
                contexts.append(dict(REQUEST_CONTEXT.get()))
                return request_id

        ids = await asyncio.gather(work(123), work(456))
        self.assertEqual({context["group_id"] for context in contexts}, {123, 456})
        self.assertEqual(len(set(ids)), 2)
        self.assertIsNone(REQUEST_CONTEXT.get())

    async def test_group_decision_enables_search_by_default_and_allows_explicit_disable(self):
        payloads = []

        def handler(request):
            payloads.append(json.loads(request.content))
            return success_response()

        call, _logs = load_transport(handler)
        group_call = call.__globals__["_call_antigravity"]
        messages = [{"role": "system", "content": "agent rules"}]
        with request_context("group_decision", 123, 90):
            await group_call(messages)
        self.assertIn("tools", payloads[-1])
        self.assertEqual(messages[0]["content"], "agent rules")
        with request_context("group_decision", 123, 90):
            REQUEST_CONTEXT.get()["backend_search"] = False
            await group_call(messages)
        self.assertNotIn("tools", payloads[-1])
        self.assertIn("search_web", payloads[-1]["messages"][0]["content"])
        self.assertNotIn("search unavailable", payloads[-1]["messages"][0]["content"])
        self.assertEqual(messages[0]["content"], "agent rules")
        with request_context("group_decision", 123, 90):
            REQUEST_CONTEXT.get()["backend_search"] = True
            await group_call(messages)
        self.assertIn("tools", payloads[-1])
        await group_call(messages)
        self.assertIn("tools", payloads[-1])
        await call(messages, enable_search=True)
        self.assertIn("tools", payloads[-1])


if __name__ == "__main__":
    unittest.main()
