import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from plugins import chat_coordination


class _FakeBot:
    self_id = "123450001"

    def __init__(self):
        self.sent: list[str] = []

    async def call_api(self, api: str, **kwargs):
        return {}

    async def get_group_member_info(self, **kwargs):
        return {"nickname": "fallback-name"}

    async def send_group_msg(self, group_id: int, message):
        self.sent.append(str(message))
        await asyncio.sleep(0)


class ChatCoordinationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        chat_coordination._group_reply_locks.clear()
        chat_coordination._last_group_batch_at.clear()
        chat_coordination._last_group_trigger_at.clear()

    async def test_raw_reply_at_bot_is_identified_as_self(self):
        event = SimpleNamespace(
            reply=None,
            raw_message="[reply:id=1816081620][at:qq=123450001]hello",
            group_id=123,
        )

        context = await chat_coordination.resolve_reply_context(_FakeBot(), event)

        self.assertIsNotNone(context)
        self.assertEqual(context.sender_id, "123450001")
        self.assertEqual(context.sender_name, "凛(我)")
        self.assertIn("回复 凛(我)", chat_coordination.format_reply_context(context, "hello"))

    async def test_reply_context_fetches_referenced_images(self):
        class ImageReplyBot(_FakeBot):
            async def call_api(self, api: str, **kwargs):
                if api == "get_msg":
                    return {
                        "sender": {"user_id": 111, "nickname": "画图的人"},
                        "message": [{
                            "type": "image",
                            "data": {"url": "https://example.com/referenced.jpg"},
                        }],
                    }
                return {}

        event = SimpleNamespace(
            reply=None,
            raw_message="[reply:id=55][at:qq=123450001]看这个",
            group_id=123,
        )

        context = await chat_coordination.resolve_reply_context(ImageReplyBot(), event)

        self.assertIsNotNone(context)
        self.assertTrue(context.has_image)
        self.assertEqual(context.image_refs[0]["url"], "https://example.com/referenced.jpg")

    async def test_concurrent_batches_do_not_interleave_and_second_is_quoted(self):
        bot = _FakeBot()
        with patch.object(chat_coordination, "REPLY_PART_DELAY_SECONDS", 0):
            await asyncio.gather(
                chat_coordination.send_group_reply_parts(
                    bot,
                    123,
                    ["A1", "A2"],
                    reply_message_id=10,
                ),
                chat_coordination.send_group_reply_parts(
                    bot,
                    123,
                    ["B1", "B2"],
                    reply_message_id=11,
                ),
            )

        self.assertEqual(bot.sent[0:2], ["A1", "A2"])
        self.assertEqual(bot.sent[2], "[CQ:reply,id=11]B1")
        self.assertEqual(bot.sent[3], "B2")


if __name__ == "__main__":
    unittest.main()
