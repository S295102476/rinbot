import base64
import io
import json
import unittest
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from PIL import Image
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed


try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", log_level="ERROR")

from plugins import image_gen  # noqa: E402


PRIMARY = "gpt-image-2.5-sunburst"
FALLBACK = "gpt-image-2"
REAL_CLIENT = httpx.AsyncClient


def png_bytes(color):
    buffer = io.BytesIO()
    Image.new("RGBA", (16, 12), color).save(buffer, format="PNG")
    return buffer.getvalue()


SOURCE = png_bytes("red")
RESULT = png_bytes("blue")
PAYLOAD = {"data": [{"b64_json": base64.b64encode(RESULT).decode()}]}


def request_fields(request):
    if request.headers["content-type"].startswith("application/json"):
        return json.loads(request.content)
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode()
        + b"\r\nMIME-Version: 1.0\r\n\r\n" + request.content
    )
    return {
        part.get_param("name", header="content-disposition"):
        part.get_payload(decode=True) if part.get_filename() else part.get_content()
        for part in message.iter_parts()
    }


class ImageFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        settings = patch.multiple(
            image_gen,
            OPENAI_IMAGE_API_URL="https://images.example.test/v1",
            OPENAI_IMAGE_API_KEY="test-image-key",
            OPENAI_IMAGE_MODEL=PRIMARY,
            OPENAI_IMAGE_EDIT_MODEL=PRIMARY,
            OPENAI_IMAGE_FALLBACK_MODEL=FALLBACK,
            OPENAI_IMAGE_EDIT_FALLBACK_MODEL=FALLBACK,
            ALLOWED_GROUPS=set(),
            STYLE_SUFFIX="",
        )
        settings.start()
        self.addCleanup(settings.stop)
        image_gen._cooldown.clear()
        self.addCleanup(image_gen._cooldown.clear)

    async def request(self, handler, **kwargs):
        async with REAL_CLIENT(transport=httpx.MockTransport(handler)) as client:
            return await image_gen._request_openai_image(
                client, "draw a blue square", group_id=100, **kwargs
            )

    async def test_success_does_not_call_fallback(self):
        calls = []

        def handler(request):
            calls.append(request_fields(request))
            self.assertEqual(request.url.path, "/v1/images/generations")
            self.assertEqual(request.headers["authorization"], "Bearer test-image-key")
            return httpx.Response(200, json=PAYLOAD)

        self.assertEqual(await self.request(handler), RESULT)
        self.assertEqual([p["model"] for p in calls], [PRIMARY])

    async def test_gateway_errors_switch_models_on_second_request(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                calls = []

                def handler(request):
                    calls.append(request_fields(request))
                    if len(calls) == 1:
                        return httpx.Response(status, text="upstream unavailable")
                    return httpx.Response(200, json=PAYLOAD)

                self.assertEqual(await self.request(handler), RESULT)
                self.assertEqual([p["model"] for p in calls], [PRIMARY, FALLBACK])
                self.assertEqual({**calls[0], "model": FALLBACK}, calls[1])

    async def test_timeout_switches_models(self):
        for error in (httpx.ConnectTimeout, httpx.ReadTimeout):
            with self.subTest(error=error):
                calls = []

                def handler(request):
                    calls.append(request_fields(request)["model"])
                    if len(calls) == 1:
                        raise error("timeout", request=request)
                    return httpx.Response(200, json=PAYLOAD)

                self.assertEqual(await self.request(handler), RESULT)
                self.assertEqual(calls, [PRIMARY, FALLBACK])

    async def test_empty_or_invalid_response_switches_models(self):
        responses = [
            httpx.Response(200, json={"data": []}),
            httpx.Response(200, json={"data": [{}]}),
            httpx.Response(200, json=["not an image response"]),
            httpx.Response(200, json={"data": [{"b64_json": "not base64"}]}),
            httpx.Response(200, json={"data": [{"b64_json": "aGVsbG8="}]}),
            httpx.Response(200, json={"data": [{"url": "data:invalid"}]}),
            httpx.Response(200, text="<html>upstream error</html>"),
        ]
        for response in responses:
            with self.subTest(body=response.content):
                calls = []

                def handler(request):
                    calls.append(request_fields(request)["model"])
                    return response if len(calls) == 1 else httpx.Response(200, json=PAYLOAD)

                self.assertEqual(await self.request(handler), RESULT)
                self.assertEqual(calls, [PRIMARY, FALLBACK])

    async def test_valid_later_image_prevents_unnecessary_fallback(self):
        calls = []

        def handler(request):
            calls.append(request_fields(request)["model"])
            return httpx.Response(200, json={"data": [None, {}, *PAYLOAD["data"]]})

        self.assertEqual(await self.request(handler), RESULT)
        self.assertEqual(calls, [PRIMARY])

    async def test_image_download_can_trigger_fallback(self):
        for failure in ("timeout", "503", "not-image"):
            with self.subTest(failure=failure):
                models = []

                def handler(request):
                    if request.method == "POST":
                        models.append(request_fields(request)["model"])
                        if len(models) == 1:
                            return httpx.Response(200, json={"data": [{"url": "https://images.example.test/result"}]})
                        return httpx.Response(200, json=PAYLOAD)
                    if failure == "timeout":
                        raise httpx.ReadTimeout("download timeout", request=request)
                    if failure == "503":
                        return httpx.Response(503)
                    return httpx.Response(200, text="not an image")

                self.assertEqual(await self.request(handler), RESULT)
                self.assertEqual(models, [PRIMARY, FALLBACK])

    async def test_edits_resend_full_image_and_prompt(self):
        calls = []

        def handler(request):
            self.assertEqual(request.url.path, "/v1/images/edits")
            calls.append(request_fields(request))
            if len(calls) == 1:
                return httpx.Response(502)
            return httpx.Response(200, json=PAYLOAD)

        with patch.object(image_gen, "OPENAI_IMAGE_EDIT_MODEL", "edit-primary"):
            self.assertEqual(await self.request(handler, png_bytes=SOURCE), RESULT)
        self.assertEqual([p["model"] for p in calls], ["edit-primary", FALLBACK])
        self.assertEqual([p["image"] for p in calls], [SOURCE, SOURCE])
        self.assertEqual({**calls[0], "model": FALLBACK}, calls[1])

    async def test_non_retryable_http_error_is_not_downgraded(self):
        for status in (400, 401, 403, 429, 451):
            with self.subTest(status=status):
                calls = []

                def handler(request):
                    calls.append(request_fields(request)["model"])
                    return httpx.Response(status, json={"error": {"code": "content_filter"}})

                with self.assertRaises(httpx.HTTPStatusError):
                    await self.request(handler)
                self.assertEqual(calls, [PRIMARY])

    async def test_failed_fallback_is_terminal(self):
        for failure, exception in (
            ("503", httpx.HTTPStatusError),
            ("timeout", httpx.ReadTimeout),
            ("empty", image_gen._NoImageError),
        ):
            with self.subTest(failure=failure):
                calls = []

                def handler(request):
                    calls.append(request_fields(request)["model"])
                    if len(calls) == 1 or failure == "503":
                        return httpx.Response(503)
                    if failure == "timeout":
                        raise httpx.ReadTimeout("timeout", request=request)
                    return httpx.Response(200, json={"data": []})

                with self.assertRaises(exception):
                    await self.request(handler)
                self.assertEqual(calls, [PRIMARY, FALLBACK])

    async def test_disabled_or_duplicate_fallback_does_not_retry_same_model(self):
        for fallback in ("", PRIMARY):
            with self.subTest(fallback=fallback):
                calls = []

                def handler(request):
                    calls.append(request_fields(request)["model"])
                    return httpx.Response(503)

                with patch.object(image_gen, "OPENAI_IMAGE_FALLBACK_MODEL", fallback):
                    with self.assertRaises(httpx.HTTPStatusError):
                        await self.request(handler)
                self.assertEqual(calls, [PRIMARY])

    async def run_command(self, mode, handler, send=None):
        bot = SimpleNamespace(self_id="999", send=send or AsyncMock())
        event = SimpleNamespace(group_id=100, user_id=123, message=Message(), reply=None)
        args = Message("draw a blue square")
        if mode == "edit":
            event.reply = SimpleNamespace(message=Message(MessageSegment.image("https://source.example.test/image.png")))
        elif mode == "avatar":
            args.append(MessageSegment.at(123))
        command = image_gen.handle_image_edit if mode == "edit" else image_gen.handle_image_gen
        transport = httpx.MockTransport(handler)
        with patch.object(image_gen.httpx, "AsyncClient", side_effect=lambda **kw: REAL_CLIENT(transport=transport, **kw)):
            await command(bot, event, args)
        return bot

    async def test_commands_use_one_fallback_and_send_one_image(self):
        for mode in ("generate", "edit", "avatar"):
            with self.subTest(mode=mode):
                image_gen._cooldown.clear()
                posts, downloads = [], []

                def handler(request):
                    if request.method == "GET":
                        downloads.append(request)
                        return httpx.Response(200, content=SOURCE)
                    posts.append(request_fields(request))
                    return httpx.Response(503) if len(posts) == 1 else httpx.Response(200, json=PAYLOAD)

                bot = await self.run_command(mode, handler)
                self.assertEqual([p["model"] for p in posts], [PRIMARY, FALLBACK])
                self.assertEqual(len(downloads), 0 if mode == "generate" else 1)
                self.assertEqual(bot.send.await_count, 2)
                result = bot.send.await_args.args[1]
                self.assertEqual([seg.type for seg in result], ["image", "text"])
                if mode != "generate":
                    self.assertEqual(posts[0]["image"], posts[1]["image"])

    async def test_commands_do_not_restart_chain_after_fallback_failure(self):
        for mode in ("generate", "edit"):
            with self.subTest(mode=mode):
                image_gen._cooldown.clear()
                models = []

                def handler(request):
                    if request.method == "GET":
                        return httpx.Response(200, content=SOURCE)
                    models.append(request_fields(request)["model"])
                    return httpx.Response(503)

                bot = await self.run_command(mode, handler)
                self.assertEqual(models, [PRIMARY, FALLBACK])
                self.assertNotIn(100, image_gen._cooldown)
                self.assertEqual(bot.send.await_count, 2)

    async def test_send_failure_does_not_generate_again(self):
        for mode in ("generate", "edit"):
            with self.subTest(mode=mode):
                image_gen._cooldown.clear()
                models = []

                def handler(request):
                    if request.method == "GET":
                        return httpx.Response(200, content=SOURCE)
                    models.append(request_fields(request)["model"])
                    return httpx.Response(200, json=PAYLOAD)

                send = AsyncMock(side_effect=[None, ActionFailed(retcode=1200, message="send timeout")])
                await self.run_command(mode, handler, send)
                self.assertEqual(models, [PRIMARY])
                self.assertEqual(send.await_count, 2)

    async def test_missing_source_image_does_not_call_either_model(self):
        requests = []

        def handler(request):
            requests.append(request.method)
            return httpx.Response(503)

        await self.run_command("edit", handler)
        self.assertEqual(requests, ["GET"])
        self.assertNotIn(100, image_gen._cooldown)


if __name__ == "__main__":
    unittest.main()
