#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
# HTTP(S)_PROXY/NO_PROXY are optional and inherited from the caller.
if [[ -f .venv/bin/activate ]]; then source .venv/bin/activate; fi
exec python bot.py
