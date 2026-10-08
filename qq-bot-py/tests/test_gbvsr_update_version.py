import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import gbvsr_fetch_overview as overview
from gbvsr_update_version import Update, merge_local


def move(input_name="5U", note="First note", zh=""):
    return {"row": {"Input": input_name}, "notes": note, "notesZh": zh,
            "aliases": [], "images": [], "hitboxes": []}


def report():
    return {"version": "2.61", "notesToReview": [], "changes": [],
            "addedMoves": [], "removedMoves": []}


class MergeTests(unittest.TestCase):
    def test_preserve_translation_despite_scraped_separators_and_noise(self):
        old = move(zh="manual translation")
        new = move(note="First note;;;2;")
        result = report()
        merge_local("Id", [new], [old], {}, result)
        self.assertEqual(new["notesZh"], old["notesZh"])
        self.assertEqual(result["notesToReview"], [])

    def test_changed_notes_do_not_keep_stale_translation(self):
        old = move(zh="old translation")
        new = move(note="Changed behavior")
        result = report()
        merge_local("Id", [new], [old], {}, result)
        self.assertEqual(new["notesZh"], "")
        self.assertEqual(result["notesToReview"][0]["oldNoteZh"], "old translation")

    def test_changed_notes_use_glossary_for_new_source(self):
        old = move(zh="old translation")
        new = move(note="New behavior;Second behavior")
        merge_local("Id", [new], [old], {"New behavior": "new", "Second behavior": "second"}, report())
        self.assertEqual(new["notesZh"], "new\nsecond")

    def test_manual_input_and_aliases_survive(self):
        old = move("j.7U/8U", zh="manual translation")
        old["aliases"] = ["j.8U", "jd"]
        new = move("j.8U")
        result = report()
        merge_local("Ferry", [new], [old], {}, result)
        self.assertEqual(new["row"]["Input"], "j.7U/8U")
        self.assertIn("jd", new["aliases"])
        self.assertEqual(result["removedMoves"], [])

    def test_duplicate_throw_translations_do_not_collide(self):
        old = [move("Ground Throw", zh="first"), move("Ground Throw", zh="second")]
        new = [move("Ground Throw"), move("Ground Throw")]
        merge_local("Nier", new, old, {}, report())
        self.assertEqual([m["notesZh"] for m in new], ["first", "second"])

    def test_keep_katalina_expanded_fields_but_update_changed_dp(self):
        old = [move("5U"), move("623U")]
        old[0]["row"]["On-Block"] = "-15/-13/-11"
        old[1]["row"]["Startup"] = "8"
        new = copy.deepcopy(old)
        new[0]["row"]["On-Block"] = "-15"
        new[1]["row"]["Startup"] = "5"
        merge_local("Katalina", new, old, {}, report())
        self.assertEqual(new[0]["row"]["On-Block"], "-15/-13/-11")
        self.assertEqual(new[1]["row"]["Startup"], "5")


class OverviewTests(unittest.TestCase):
    def test_state_rows_never_overwrite_default_but_ex_is_distinct(self):
        rows = [
            ("Sandalphon", "Sandalphon (Wind-Aligned)", "11"),
            ("Sandalphon", "Sandalphon", "9"),
            ("Siegfried", "Siegfried", "5.8"),
            ("Siegfried", "Siegfried (Blood of the Dragon)", "99"),
            ("Gran", "Gran", "8"),
            ("Gran_(EX)", "Gran (EX)", "8.5"),
        ]
        html = "<table><thead><tr><th>Character</th><th>Walk Speed</th></tr></thead><tbody>"
        for href, label, value in rows:
            html += f'<tr><td><a href="/w/GBVSR/{href}">{label}</a></td><td>{value}</td></tr>'
        html += "</tbody></table>"
        parsed = overview._extract_table_rows(html)
        self.assertEqual(parsed["Sandalphon"]["stats"]["walk_speed"], "9")
        self.assertEqual(parsed["Siegfried"]["stats"]["walk_speed"], "5.8")
        self.assertEqual(parsed["Gran_(EX)"]["stats"]["walk_speed"], "8.5")


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_upstream_image_keeps_existing_local_original(self):
        with tempfile.TemporaryDirectory() as directory:
            update = Update(SimpleNamespace(version="2.61", snapshot="20260922"))
            update.root = Path(directory)
            update.stage = update.root / "staged"
            path = Path("data/gbvsr/images/Id/test.png")
            (update.root / path).parent.mkdir(parents=True)
            Image.new("RGB", (2, 2)).save(update.root / path)
            update.imageinfo = AsyncMock(return_value={"test.png": {}, "missing.png": {}})
            row = move()
            row["images"] = [{"filename": "test.png"}, {"filename": "missing.png"}]
            await update.localize(None, {"Id": {"rows": [row]}}, {})
            self.assertEqual(row["imagePaths"], [path.as_posix()])
            self.assertEqual(len(update.report["missingImages"]), 2)
            self.assertEqual(update.report["imageFailures"], [])


if __name__ == "__main__":
    unittest.main()
