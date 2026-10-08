"""Start the bot with a checked configuration; preserve seeded named volumes."""
from pathlib import Path
import os
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

for name in ("config.yaml", ".env"):
    if not (ROOT / name).is_file():
        raise SystemExit("Missing runtime configuration: run docker compose run --rm init first")

# Named volumes are initially seeded by Docker from the image. Subsequent starts
# retain all persona edits, local resources and runtime data; never copy over them.
from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=False)

from runtime_config import load_config, validate_config
try:
    validate_config(load_config())
except ValueError as exc:
    raise SystemExit(str(exc)) from None

runpy.run_path(str(ROOT / "bot.py"), run_name="__main__")
