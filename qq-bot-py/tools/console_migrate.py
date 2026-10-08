"""Add console tables, or check their schema without changing the database."""

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect
from plugins import db
from plugins.console_services import CONSOLE_SERVICE_TABLES
from plugins.console_state import CONSOLE_TABLES, initialize_console


async def migrate(check_only: bool) -> int:
    try:
        if not check_only:
            await initialize_console()
        def check(connection):
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            missing = []
            for table in CONSOLE_TABLES + CONSOLE_SERVICE_TABLES:
                if table.name not in tables:
                    missing.append(table.name)
                    continue
                columns = {item["name"] for item in inspector.get_columns(table.name)}
                missing.extend(f"{table.name}.{column.name}" for column in table.columns if column.name not in columns)
            return missing
        async with db.engine.connect() as connection:
            missing = await connection.run_sync(check)
        if missing:
            print("Missing console schema: " + ", ".join(missing))
            return 1
        print("Console schema is ready. Existing business data was not reset.")
        return 0
    except Exception as exc:
        print(f"Console schema unavailable ({type(exc).__name__}); check database connectivity and privileges.")
        return 2
    finally:
        await db.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Inspect only; do not create tables or seed settings")
    raise SystemExit(asyncio.run(migrate(parser.parse_args().check)))
