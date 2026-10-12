import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init(driver="~fastapi", log_level="ERROR")

with patch("sqlalchemy.ext.asyncio.create_async_engine"):
    from plugins import gbvsr_frame as frame


def character_data(name):
    # Minimal published fixture: aliases require move inputs, not private GBVSR
    # downloads, translated notes or the multi-gigabyte image collection.
    path = Path(__file__).resolve().parent / "fixtures/gbvsr" / f"{name.lower()}Frame2.local.json"
    return json.loads(path.read_text(encoding="utf-8"))


class FollowupQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.indices = {
            name: frame._build_index(character_data(name)["rows"], name)
            for name in ("Lucilius", "Wilnas")
        }

    def find(self, character, query):
        return {
            item["row"]["Input"]
            for token in frame._expand_query_token(query)
            for item in frame._find_items(self.indices[character], token)
        }

    def test_lucilius_unique_action_includes_all_followups(self):
        expected = {"5U", "5U~U", "5U~L", "5U~M", "5U~H"}
        for query in ("5d", "5D", "5u", "5U"):
            with self.subTest(query=query):
                self.assertEqual(self.find("Lucilius", query), expected)

    def test_lucilius_explicit_followup_selects_only_that_move(self):
        for button, shortcut in (("U", "d"), ("L", "a"), ("M", "b"), ("H", "c")):
            for query in (f"5U~{button}", f"5d~{shortcut}", f"5u~{shortcut}"):
                with self.subTest(query=query):
                    self.assertEqual(self.find("Lucilius", query), {f"5U~{button}"})

    def test_wilnas_dash_followups_have_button_shortcuts(self):
        for button, shortcut in (("L", "a"), ("M", "b"), ("H", "c")):
            for query in (f"66{shortcut}", f"66{shortcut.upper()}", f"66{button}",
                          f"66~{button}", f"66~{shortcut}"):
                with self.subTest(query=query):
                    self.assertEqual(self.find("Wilnas", query), {f"66~{button}"})

    def test_base_moves_and_other_buttons_remain_separate(self):
        self.assertEqual(self.find("Wilnas", "5d"), {"5U"})
        self.assertEqual(self.find("Wilnas", "66"), {"66"})
        self.assertEqual(self.find("Wilnas", "66d"), set())
        self.assertEqual(self.find("Lucilius", "66a"), {"66L"})
        self.assertEqual(self.find("Lucilius", "66b"), {"66M"})

    def test_motion_and_air_notation_remain_supported(self):
        aliases = frame._input_aliases("j.214U~L")
        self.assertTrue({"j24d", "j214u", "j24d~a", "j214u~l"}.issubset(aliases))
        self.assertNotIn("j214ul", aliases)
        self.assertNotIn("214u", aliases)
        self.assertTrue({"236u", "26d"}.issubset(frame._input_aliases("236U~X")))


class FollowupHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_send_all_distinct_moves_in_forward_message(self):
        cases = (
            ("Lucilius", "5d", {"5U", "5U~U", "5U~L", "5U~M", "5U~H"}),
            ("Wilnas", "66a/66b/66c", {"66~L", "66~M", "66~H"}),
        )
        for character, query, expected in cases:
            with self.subTest(character=character):
                data = character_data(character)
                loaded = {"data": data, "index": frame._build_index(data["rows"], character)}
                bot = SimpleNamespace(self_id="1", call_api=AsyncMock(), send=AsyncMock())
                matcher = SimpleNamespace(finish=AsyncMock())
                event = SimpleNamespace(group_id=123)
                with patch.object(frame, "_load_character_from_db", AsyncMock(return_value=loaded)), \
                     patch.object(frame, "_build_node", side_effect=lambda _bot, _char, item: {
                         "input": item["row"]["Input"]}):
                    await frame._handle_gb_frame_query(bot, event, f"{character} {query}", matcher)
                bot.call_api.assert_awaited_once()
                self.assertEqual(bot.call_api.await_args.args, ("send_group_forward_msg",))
                nodes = bot.call_api.await_args.kwargs["messages"]
                self.assertEqual(len(nodes), len(expected))
                self.assertEqual({node["input"] for node in nodes}, expected)
                bot.send.assert_not_awaited()
                matcher.finish.assert_not_awaited()

    async def test_db_loading_rebuilds_aliases_without_reimport(self):
        rows = [frame.GBVSRFrameMove(
            character="Wilnas", section="Unique Actions", input_name=f"66~{button}",
            move_name=f"Advancing {button}", notes="", notes_zh="",
            image_paths="[]", hitbox_paths="[]", aliases="[]",
        ) for button in ("L", "M", "H")]
        result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
        session = AsyncMock()
        session.execute.return_value = result
        context = AsyncMock()
        context.__aenter__.return_value = session
        with patch.object(frame, "SessionFactory", return_value=context), patch.object(frame, "_CACHE", {}):
            loaded = await frame._load_character_from_db("Wilnas")
        for button, query in (("L", "66a"), ("M", "66b"), ("H", "66c")):
            token = frame._normalize_query_token(query)
            found = frame._find_items(loaded["index"], token)
            self.assertEqual([item["row"]["Input"] for item in found], [f"66~{button}"])


if __name__ == "__main__":
    unittest.main()
