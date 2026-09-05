"""Apply pending database migrations. Safe to run on every deploy."""

import asyncio
import sys

sys.path.insert(0, "src")

from coinfinder.db import migrate
from coinfinder.logging_setup import setup_logging


async def main() -> None:
    setup_logging()
    applied = await migrate()
    print(f"applied {len(applied)} migration(s): {applied or 'none (up to date)'}")


if __name__ == "__main__":
    asyncio.run(main())
