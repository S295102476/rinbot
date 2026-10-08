import unittest
from types import SimpleNamespace

import nonebot


try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi")

from plugins.web_search import _extract_search_query, _parse_search_trigger  # noqa: E402


def _event(plain: str, raw: str | None = None):
    return SimpleNamespace(
        get_plaintext=lambda: plain,
        raw_message=plain if raw is None else raw,
        to_me=True,
    )


class WebSearchTriggerTests(unittest.TestCase):
    def test_plain_sentence_with_search_character_does_not_trigger(self):
        event = _event("我是来搜集你的情报的")
        self.assertIsNone(_extract_search_query(event))

    def test_at_bot_without_literal_name_does_not_trigger(self):
        event = _event("搜 relink", "[CQ:at,qq=123456]搜 relink")
        self.assertIsNone(_extract_search_query(event))

    def test_literal_name_and_search_verb_trigger(self):
        self.assertEqual(_parse_search_trigger("凛，帮我搜 relink 轮椅"), "relink 轮椅")

    def test_raw_message_keeps_name_when_plaintext_was_preprocessed(self):
        event = _event("帮我搜 relink", "凛，帮我搜 relink")
        self.assertEqual(_extract_search_query(event), "relink")

    def test_search_compound_is_not_treated_as_command(self):
        self.assertIsNone(_parse_search_trigger("凛，我是来搜集你的情报的"))

    def test_search_word_later_in_a_normal_sentence_does_not_trigger(self):
        self.assertIsNone(
            _parse_search_trigger("凛，自己去调查总比我说的有道理，你去查吧")
        )

    def test_reply_markup_does_not_turn_normal_sentence_into_search(self):
        event = _event(
            "自己去调查总比我说的有道理，你去查吧",
            "[CQ:reply,id=1][CQ:at,qq=123456]"
            "凛，自己去调查总比我说的有道理，你去查吧",
        )
        self.assertIsNone(_extract_search_query(event))


if __name__ == "__main__":
    unittest.main()
