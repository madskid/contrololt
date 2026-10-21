"""Worker terpisah:  python -m app.worker   (jalankan SATU instance saja)."""
import asyncio
import logging

from app.db import init_db, wait_for_db
from app.services.runtime import run_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


async def main() -> None:
    await wait_for_db()
    await init_db()
    await run_forever()


if __name__ == "__main__":
    asyncio.run(main())
