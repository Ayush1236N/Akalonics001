#!/usr/bin/env bash
set -euo pipefail
# Wait for postgres, apply migrations, then exec the given command.
if [[ -n "${DATABASE_URL:-}" ]]; then
  for i in $(seq 1 30); do
    python -c "
import asyncio, sys
from sqlalchemy.ext.asyncio import create_async_engine
from config import settings
async def main():
    e = create_async_engine(settings.sqlalchemy_url)
    async with e.connect() as c: pass
    await e.dispose()
asyncio.run(main())
" && break || sleep 2
  done
  alembic upgrade head
fi
exec "$@"
