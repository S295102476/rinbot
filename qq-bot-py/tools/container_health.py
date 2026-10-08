"""Ready means built UI + protected API, not a QQ login or model quota check."""
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def main() -> int:
    try:
        with urlopen("http://127.0.0.1:8080/admin/", timeout=3) as response:
            if response.status != 200 or b"<html" not in response.read(4096).lower():
                return 1
        try:
            urlopen("http://127.0.0.1:8080/api/admin/auth/me", timeout=3).close()
        except HTTPError as exc:
            return 0 if exc.code == 401 else 1
        return 1  # An anonymous successful auth check is never healthy.
    except (HTTPError, URLError, TimeoutError, OSError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
