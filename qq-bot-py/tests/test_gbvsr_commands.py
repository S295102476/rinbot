import unittest
from types import SimpleNamespace
from unittest.mock import patch

import nonebot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.rule import CommandRule, TrieRule

nonebot.init(driver="~fastapi", log_level="ERROR")

with patch("sqlalchemy.ext.asyncio.create_async_engine"):
    from plugins import gbvsr_frame as frame


class CommandCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_case_variants_reach_the_same_command_matcher(self):
        command_rule = next(
            checker.call for checker in frame.gb_cmd.rule.checkers
            if isinstance(checker.call, CommandRule)
        )
        bot = SimpleNamespace(self_id="1")
        for command in ("#GB", "#gb", "#Gb", "#gB"):
            for args in ("Id 2424c", "\u5361\u59d0 24b", "\u5e2e\u52a9", "Id \u6982\u89c8"):
                with self.subTest(command=command, args=args):
                    message = Message(f"{command} {args}")
                    event = SimpleNamespace(get_type=lambda: "message", get_message=lambda: message)
                    parsed = TrieRule.get_value(bot, event, {})
                    self.assertEqual(parsed["raw_command"], command)
                    self.assertEqual(parsed["command_arg"].extract_plain_text(), args)
                    self.assertTrue(await command_rule(
                        cmd=parsed["command"], cmd_arg=parsed["command_arg"],
                        cmd_whitespace=parsed["command_whitespace"],
                    ))
        self.assertTrue(frame.gb_cmd.block)

    async def test_at_entry_remains_case_insensitive(self):
        at_rule = next(iter(frame.gb_at_cmd.rule.checkers)).call
        bot = SimpleNamespace(self_id="1")
        for command in ("#GB", "#gb", "#Gb", "#gB"):
            with self.subTest(command=command):
                message = MessageSegment.at("1") + Message(f" {command} Id 2424c")
                event = SimpleNamespace(message=message, get_plaintext=message.extract_plain_text)
                self.assertTrue(await at_rule(bot, event))
                self.assertEqual(frame._extract_gb_arg_from_plain(event.get_plaintext()), "Id 2424c")
        self.assertTrue(frame.gb_at_cmd.block)


if __name__ == "__main__":
    unittest.main()
