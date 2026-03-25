"""
app/db/client.py
────────────────
Single Supabase client instance shared across the entire app.
Uses the service role key — has full DB access, bypasses RLS.
Never expose this key or client to end users.
"""

from functools import lru_cache

from supabase import Client, create_client

from app.config import get_settings


@lru_cache(maxsize=1)
def get_db() -> Client:
    """
    Returns a cached Supabase client.
    Called once at startup; reused for every request.
    """
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_key)
