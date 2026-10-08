"""Generate a console password hash without accepting a password in argv."""

from getpass import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.console_auth import hash_password


def main() -> None:
    password = getpass("Console password (at least 12 characters): ")
    confirmation = getpass("Repeat password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    try:
        encoded = hash_password(password)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print("AGENT_CONSOLE_PASSWORD_HASH=" + encoded)


if __name__ == "__main__":
    main()
