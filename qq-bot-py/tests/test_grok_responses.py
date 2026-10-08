import unittest

import httpx

from plugins.responses_api import (
    ResponsesAPIError,
    ResponsesConfig,
    build_responses_payload,
    call_responses,
    messages_to_responses_input,
    parse_responses_text,
)


CFG = ResponsesConfig(
    name="OpenAI Responses",
    model="gpt-5.6-sol",
    base_url="https://example.test/v1",
    api_key="test-key",
    api_backend="responses",
    supports_backend_search=True,
    timeout=120,
)


class ResponsesTests(unittest.IsolatedAsyncioTestCase):
    def test_messages_are_split_into_instructions_and_input(self):
        instructions, input_items = messages_to_responses_input([
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ])
        self.assertEqual(instructions, "persona")
        self.assertEqual(input_items, [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ])

    def test_search_tool_is_opt_in(self):
        plain = build_responses_payload(CFG, [{"role": "user", "content": "hi"}])
        searched = build_responses_payload(
            CFG,
            [{"role": "user", "content": "latest news"}],
            enable_web_search=True,
        )
        self.assertNotIn("tools", plain)
        self.assertEqual(searched["tools"], [{"type": "web_search"}])

    def test_max_tokens_maps_to_responses_field(self):
        payload = build_responses_payload(
            CFG,
            [{"role": "user", "content": "hi"}],
            max_tokens=321,
        )
        self.assertEqual(payload["max_output_tokens"], 321)
        self.assertNotIn("max_tokens", payload)

    def test_search_rejects_disabled_backend(self):
        disabled = ResponsesConfig(**{**CFG.__dict__, "supports_backend_search": False})
        with self.assertRaises(ResponsesAPIError):
            build_responses_payload(
                disabled,
                [{"role": "user", "content": "news"}],
                enable_web_search=True,
            )

    def test_parse_top_level_output_text(self):
        self.assertEqual(parse_responses_text({"output_text": " pong "}), "pong")

    def test_parse_nested_output_text(self):
        data = {
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "hello"}],
            }]
        }
        self.assertEqual(parse_responses_text(data), "hello")

    def test_empty_response_text(self):
        self.assertEqual(parse_responses_text({"output": []}), "")

    async def test_non_object_json_is_explicit(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["not", "an", "object"])

        config = {"ai": {"openai_responses": {**CFG.__dict__}}}
        with self.assertRaisesRegex(ResponsesAPIError, "invalid JSON body"):
            await call_responses(
                config,
                [{"role": "user", "content": "hi"}],
                transport=httpx.MockTransport(handler),
            )

    async def test_http_error_is_explicit(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertNotIn("test-key", str(request.url))
            return httpx.Response(429, json={"error": {"message": "busy"}})

        config = {
            "ai": {
            "openai_responses": {
                    **CFG.__dict__,
                }
            }
        }
        with self.assertRaisesRegex(ResponsesAPIError, r"HTTP 429.*busy"):
            await call_responses(
                config,
                [{"role": "user", "content": "hi"}],
                transport=httpx.MockTransport(handler),
            )

    async def test_empty_text_raises(self):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "completed", "output": []})

        config = {"ai": {"openai_responses": {**CFG.__dict__}}}
        with self.assertRaisesRegex(ResponsesAPIError, "no text"):
            await call_responses(
                config,
                [{"role": "user", "content": "hi"}],
                transport=httpx.MockTransport(handler),
            )


if __name__ == "__main__":
    unittest.main()
