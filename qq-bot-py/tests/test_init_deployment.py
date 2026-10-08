"""No real credentials, network calls or bot plugin imports."""
import base64
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

_SPEC = importlib.util.spec_from_file_location("init_deployment", Path(__file__).resolve().parents[1] / "tools/init_deployment.py")
init = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = init
_SPEC.loader.exec_module(init)


class InitDeploymentTests(unittest.TestCase):
    def answers(self, **updates):
        values = dict(api_url="http://model.invalid/v1/chat/completions", api_key="ci-only-key",
                      model="test-model", admin=10001, groups=(10002, 10003), console_password="test$only'password")
        values.update(updates)
        return init.Answers(**values)

    def test_generated_credentials_and_access_scope_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init.initialize(root, self.answers())
            config = yaml.safe_load((root / "runtime/config.yaml").read_text(encoding="utf-8"))
            compose = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines())
            bot = dict(line.split("=", 1) for line in (root / "runtime/bot.env").read_text().splitlines())
            self.assertEqual(config["database"]["password"], compose["MYSQL_PASSWORD"].strip("'"))
            self.assertEqual(config["redis"]["password"], compose["REDIS_PASSWORD"].strip("'"))
            self.assertEqual(config["meme"]["minio"]["secret_key"], compose["MINIO_ROOT_PASSWORD"].strip("'"))
            self.assertNotEqual(config["database"]["password"], config["redis"]["password"])
            self.assertEqual(config["allowed_groups"], [10002, 10003])
            self.assertEqual(config["agent"]["active_groups"], config["allowed_groups"])
            self.assertEqual(config["agent"]["dev"]["admin_users"], [10001])
            self.assertFalse(config["features"]["gsuid"])
            encoded = bot["AGENT_CONSOLE_PASSWORD_HASH"].strip("'")
            _, _, n, r, p, salt, expected = encoded.split("$")
            actual = hashlib.scrypt(self.answers().console_password.encode(), salt=base64.urlsafe_b64decode(salt),
                                    n=int(n), r=int(r), p=int(p), dklen=32)
            self.assertEqual(actual, base64.urlsafe_b64decode(expected))
            self.assertTrue(bot["AGENT_CONSOLE_PASSWORD_HASH"].startswith("'scrypt$"))
            self.assertNotIn(self.answers().console_password, (root / "runtime/bot.env").read_text())
            originals = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with self.assertRaises(FileExistsError):
                init.initialize(root, self.answers())
            self.assertEqual(originals, {p: p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_invalid_answers_write_nothing(self):
        for patch in ({"api_url": "http://user:secret@host/v1/chat/completions"},
                      {"api_url": "file:///tmp/model"}, {"api_key": ""}, {"model": "bad\nmodel"},
                      {"groups": ()}, {"admin": 0}, {"console_password": "short"}):
            with self.subTest(patch=patch), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    init.initialize(Path(directory), self.answers(**patch))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_existing_partial_or_legacy_configuration_is_never_overwritten(self):
        for relative in (".env", "runtime", "qq-bot-py/.env", "qq-bot-py/config.yaml"):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private data", encoding="utf-8")
                with self.assertRaises(FileExistsError):
                    init.initialize(root, self.answers())
                self.assertEqual(path.read_text(), "private data")

    def test_group_ids_are_validated_and_deduplicated(self):
        self.assertEqual(init.parse_ids("10002,10003 10002"), (10002, 10003))
        for value in ("", "0", "10002,not-a-number", "-10", "9223372036854775808"):
            with self.assertRaises(ValueError):
                init.parse_ids(value)


if __name__ == "__main__":
    unittest.main()
