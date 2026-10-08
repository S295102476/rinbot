import unittest

from plugins.meme_policy import group_is_enabled, resolve_group_rate


class MemeCollectionPolicyTests(unittest.TestCase):
    def test_collection_only_enabled_for_group_chat_whitelist(self):
        enabled_groups = {10001, 10002}

        self.assertTrue(group_is_enabled(10001, enabled_groups))
        self.assertTrue(group_is_enabled(10002, enabled_groups))
        self.assertFalse(group_is_enabled(10003, enabled_groups))

    def test_explicit_zero_rate_is_preserved_for_numeric_group_key(self):
        overrides = {
            10003: {"collect_rate": 0},
        }

        self.assertEqual(
            resolve_group_rate(overrides, 10003, "collect_rate", 0.25),
            0,
        )

    def test_explicit_zero_rate_is_preserved_for_string_group_key(self):
        overrides = {
            "10003": {"collect_rate": 0},
        }

        self.assertEqual(
            resolve_group_rate(overrides, 10003, "collect_rate", 0.25),
            0,
        )


if __name__ == "__main__":
    unittest.main()
