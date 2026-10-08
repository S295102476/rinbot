import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot


nonebot.init(driver="~fastapi")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.gsuid_bridge import (  # noqa: E402
    _is_core_command,
    _is_direct_only_core_command,
    _is_game_command,
    _normalize_core_command_text,
    _send_unsolicited,
)


class GsuidBridgeCommandTests(unittest.TestCase):
    def test_device_commands_are_forwarded(self):
        for command in (
            "绑定设备",
            "设备登录",
            "设备登陆",
            "mys设备登录",
            "mys设备登陆",
            "mys绑定设备",
        ):
            with self.subTest(command=command):
                self.assertTrue(_is_game_command(command))

    def test_regular_text_is_not_forwarded(self):
        self.assertFalse(_is_game_command("你好"))

    def test_core_command_detection_is_separate_from_game_commands(self):
        self.assertTrue(_is_core_command("绑定设备"))
        self.assertTrue(_is_core_command("core绑定设备"))
        self.assertFalse(_is_core_command("zzz查询"))

    def test_only_device_commands_require_a_private_message(self):
        self.assertTrue(_is_direct_only_core_command("绑定设备"))
        self.assertTrue(_is_direct_only_core_command("绑定设备 {\"fp\": \"...\"}"))
        self.assertTrue(_is_direct_only_core_command("core绑定设备"))
        self.assertFalse(_is_direct_only_core_command("绑定信息"))

    def test_bare_device_command_uses_documented_gscore_command(self):
        self.assertEqual(_normalize_core_command_text("绑定设备"), "mys设备登录")
        self.assertEqual(
            _normalize_core_command_text("绑定设备 {\"fp\": \"x\"}"),
            "mys设备登录 {\"fp\": \"x\"}",
        )
        self.assertEqual(_normalize_core_command_text("mys绑定设备"), "mys绑定设备")
        self.assertEqual(_normalize_core_command_text("zzz查询"), "zzz查询")


class GsuidBridgePushTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_push_is_delivered_without_pending_request(self):
        bot = type("FakeBot", (), {"self_id": "123450001"})()
        bot.send_private_msg = AsyncMock()
        data = {
            "bot_id": "Nonebot",
            "bot_self_id": "123450001",
            "target_type": "direct",
            "target_id": "123456789",
            "content": [{"type": "text", "data": "抽卡登录成功"}],
        }

        with patch("plugins.gsuid_bridge.get_bots", return_value={"nonebot": bot}):
            self.assertTrue(await _send_unsolicited(data))

        bot.send_private_msg.assert_awaited_once()
        self.assertEqual(bot.send_private_msg.await_args.kwargs["user_id"], 123456789)

    async def test_group_push_is_delivered_without_pending_request(self):
        bot = type("FakeBot", (), {"self_id": "123450001"})()
        bot.send_group_msg = AsyncMock()
        data = {
            "bot_id": "Nonebot",
            "target_type": "group",
            "target_id": "987654321",
            "content": [{"type": "text", "data": "插件更新完成"}],
        }

        with patch("plugins.gsuid_bridge.get_bots", return_value={"nonebot": bot}):
            self.assertTrue(await _send_unsolicited(data))

        bot.send_group_msg.assert_awaited_once()
        self.assertEqual(bot.send_group_msg.await_args.kwargs["group_id"], 987654321)

    async def test_chat_only_group_push_is_suppressed(self):
        bot = type("FakeBot", (), {"self_id": "123450001"})()
        bot.send_group_msg = AsyncMock()
        data = {
            "bot_id": "Nonebot",
            "target_type": "group",
            "target_id": "987650001",
            "content": [{"type": "text", "data": "插件更新完成"}],
        }

        with patch("plugins.gsuid_bridge.get_bots", return_value={"nonebot": bot}):
            self.assertTrue(await _send_unsolicited(data))

        bot.send_group_msg.assert_not_awaited()

    async def test_log_frame_is_not_sent_as_a_user_message(self):
        data = {
            "bot_id": "Nonebot",
            "target_type": None,
            "target_id": None,
            "content": [{"type": "log", "data": {"level": "INFO", "msg": "ok"}}],
        }

        with patch("plugins.gsuid_bridge.get_bots", return_value={}):
            self.assertFalse(await _send_unsolicited(data))


if __name__ == "__main__":
    unittest.main()
