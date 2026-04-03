"""
app/db/client.py
────────────────
Single async Supabase client instance shared across the app.
Uses the service role key — has full DB access, bypasses RLS.
Never expose this key or client to end users.
"""

import asyncio

from supabase import AsyncClient, acreate_client

from app.config import get_settings

_db_client: AsyncClient | None = None
_db_lock = asyncio.Lock()


async def get_db() -> AsyncClient:
    """
    Return a lazily created cached async Supabase client.

    Safe under concurrent startup requests.
    """
    global _db_client

    if _db_client is not None:
        return _db_client

    async with _db_lock:
        if _db_client is None:
            settings = get_settings()
            _db_client = await acreate_client(settings.supabase_url, settings.supabase_service_key)

    return _db_client
