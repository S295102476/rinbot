import unittest

from plugins.translation_routing import select_translation_provider


class TranslateRoutingTests(unittest.TestCase):
    def test_text_uses_antigravity(self):
        self.assertEqual(select_translation_provider(None), "primary")

    def test_image_uses_gemini(self):
        self.assertEqual(select_translation_provider(b"image"), "primary")


if __name__ == "__main__":
    unittest.main()
