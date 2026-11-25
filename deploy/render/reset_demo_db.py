"""Empty the demo database before migrations, on boot (deploy/render/start.sh).

On Render's free tier the artifact store lives on the container's disk, which
starts empty after every restart, deploy or idle spin-down. Dropping the demo
data at the same moment keeps the database from pointing at files that are
gone. Visitors start from an empty instance and click "Start tour".

Refuses to touch anything but the demo database in demo mode.
"""
import asyncio
import os
import sys

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import DEMO_DATABASE


async def reset(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


def main() -> int:
    url = os.environ.get("SFCI_DATABASE_URL", "")
    if os.environ.get("SFCI_DEMO_MODE") != "true":
        print("reset_demo_db: refusing, SFCI_DEMO_MODE is not true", file=sys.stderr)
        return 2
    if not url or make_url(url).database != DEMO_DATABASE:
        print(f"reset_demo_db: refusing, the database is not {DEMO_DATABASE!r}", file=sys.stderr)
        return 2
    asyncio.run(reset(url))
    print(f"reset_demo_db: emptied {DEMO_DATABASE!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
