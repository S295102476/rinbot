import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot

nonebot.init(driver="~fastapi", log_level="ERROR")

# Queries are tested with real ORM rows, but without a driver or DB connection.
with patch("sqlalchemy.ext.asyncio.create_async_engine"):
    from plugins import gbvsr_frame as frame


ROOT = Path(__file__).resolve().parents[1]
ID_PATH = ROOT / "tests/fixtures/gbvsr/idFrame2.local.json"


class DragonQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = json.loads(ID_PATH.read_text(encoding="utf-8"))["rows"]
        cls.index = frame._build_index(cls.rows, "Id")

    def find(self, command, index=None):
        index = self.index if index is None else index
        return {
            item["row"]["Input"]
            for token in frame._expand_query_token(command)
            for item in frame._find_items(index, token)
        }

    def test_chinese_dragon_queries_select_only_the_requested_move(self):
        cases = {
            "df.L": ("龙5a", "龙a", "龙l", "龙5L"),
            "df.M": ("龙5b", "龙b", "龙m", "龙5M"),
            "df.H": ("龙5c", "龙c", "龙h", "龙5H"),
            "df.U": ("龙5d", "龙d", "龙u", "龙5U"),
            "df.5S": ("龙s", "龙5s", "龙S", "龙5S"),
            "df.LU": ("龙投", "龙LU", "龙lu"),
        }
        for expected, commands in cases.items():
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual(self.find(command), {expected})

    def test_raw_dustloop_inputs_still_work(self):
        for item in self.rows:
            name = item["row"]["Input"]
            if name.startswith("df."):
                with self.subTest(input=name):
                    self.assertEqual(self.find(name), {name})

    def test_transformation_super_includes_all_dragon_moves(self):
        expected = {item["row"]["Input"] for item in self.rows
                    if item["row"]["Input"].startswith("df.")}
        expected.add("214214H")
        self.assertEqual(len(expected), 7)
        for command in ("2424c", "2424C", "214214H", "214214c", "2424h"):
            with self.subTest(command=command):
                self.assertEqual(self.find(command), expected)

    def test_regular_normals_and_other_supers_do_not_include_dragon_moves(self):
        self.assertEqual(self.find("5a"), {"c.L", "f.L"})
        self.assertEqual(self.find("2626c"), {"236236H"})
        self.assertEqual(self.find("2424d"), set())

    def test_aliases_are_scoped_to_id(self):
        index = frame._build_index(self.rows, "Vira")
        self.assertEqual(self.find("龙a", index), set())
        self.assertEqual(self.find("2424c", index), {"214214H"})

    def test_json_loading_builds_character_specific_index(self):
        with patch.object(frame, "_CACHE", {}), patch.object(frame, "_FRAME_FILE_MAP", {"id": ID_PATH}):
            loaded = frame._load_character("Id")
        self.assertEqual(self.find("龙投", loaded["index"]), {"df.LU"})


class DragonHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_query_sends_one_forward_with_seven_distinct_nodes(self):
        data = json.loads(ID_PATH.read_text(encoding="utf-8"))
        loaded = {"data": data, "index": frame._build_index(data["rows"], "Id")}
        bot = SimpleNamespace(self_id="1", call_api=AsyncMock(), send=AsyncMock())
        matcher = SimpleNamespace(finish=AsyncMock())
        event = SimpleNamespace(group_id=123)

        def node(_bot, _character, item):
            return {"input": item["row"]["Input"]}

        with patch.object(frame, "_load_character_from_db", AsyncMock(return_value=loaded)), \
             patch.object(frame, "_build_node", side_effect=node):
            await frame._handle_gb_frame_query(bot, event, "伊德 2424C/龙投/龙a", matcher)

        bot.call_api.assert_awaited_once()
        self.assertEqual(bot.call_api.await_args.args, ("send_group_forward_msg",))
        nodes = bot.call_api.await_args.kwargs["messages"]
        self.assertEqual(len(nodes), 7)
        self.assertEqual(nodes[0]["input"], "214214H")
        self.assertEqual(len({n["input"] for n in nodes}), 7)
        bot.send.assert_not_awaited()
        matcher.finish.assert_not_awaited()

    async def test_database_loading_builds_character_specific_index(self):
        row = frame.GBVSRFrameMove(
            character="Id", section="Skybound Arts", input_name="df.LU",
            move_name="Unbound Slam", notes="", notes_zh="",
            image_paths="[]", hitbox_paths="[]", aliases='["dflu"]',
        )
        result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))
        session = AsyncMock()
        session.execute.return_value = result
        context = AsyncMock()
        context.__aenter__.return_value = session
        with patch.object(frame, "SessionFactory", return_value=context), patch.object(frame, "_CACHE", {}):
            loaded = await frame._load_character_from_db("Id")
        self.assertEqual([i["row"]["Input"] for i in frame._find_items(loaded["index"], "龙投")], ["df.LU"])
        self.assertEqual([i["row"]["Input"] for i in frame._find_items(loaded["index"], "2424h")], ["df.LU"])


if __name__ == "__main__":
    unittest.main()
