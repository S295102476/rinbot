import unittest

from plugins.provider_output import (
    extract_gemini_text,
    extract_json_object,
    recover_malformed_replies,
    strip_inline_citation_markers,
    strip_tool_activity,
)


SEARCH_NOTICE = "🔍 已为您搜索： relink 轮椅, relink maglielle wheelchair"


class ProviderOutputTests(unittest.TestCase):
    def test_normal_reply_punctuation_is_unchanged(self):
        self.assertEqual(strip_tool_activity("普通回答，"), "普通回答，")

    def test_search_notice_is_removed_from_valid_json(self):
        raw = '{"replies":["这是最终答案"]}，' + SEARCH_NOTICE

        self.assertEqual(strip_tool_activity(raw), '{"replies":["这是最终答案"]}')
        self.assertEqual(extract_json_object(raw), {"replies": ["这是最终答案"]})

    def test_single_backtick_json_wrapper_is_parsed(self):
        raw = '`json\n{"replies":["自然地说一句"]}\n`'

        self.assertEqual(
            extract_json_object(raw),
            {"replies": ["自然地说一句"]},
        )

    def test_malformed_replies_json_recovers_answer_without_scaffolding(self):
        raw = '{"replies":","不过兰斯洛特更适合无脑右键。"]}，' + SEARCH_NOTICE

        self.assertEqual(
            recover_malformed_replies(raw),
            ["不过兰斯洛特更适合无脑右键。"],
        )

    def test_gemini_extractor_skips_thought_and_search_activity(self):
        data = {
            "candidates": [{
                "content": {
                    "parts": [
                        {"text": "内部推理过程", "thought": True},
                        {"text": '{"replies":["最终结果"]}，' + SEARCH_NOTICE},
                    ]
                }
            }]
        }

        self.assertEqual(extract_gemini_text(data), '{"replies":["最终结果"]}')

    def test_inline_multilevel_citation_is_removed_before_punctuation(self):
        self.assertEqual(
            strip_inline_citation_markers("展开拐得也太快了吧 [1.1.2]。"),
            "展开拐得也太快了吧。",
        )

    def test_non_citation_bracket_text_is_preserved(self):
        self.assertEqual(
            strip_inline_citation_markers("数组内容是[1, 1, 2]。"),
            "数组内容是[1, 1, 2]。",
        )


if __name__ == "__main__":
    unittest.main()
