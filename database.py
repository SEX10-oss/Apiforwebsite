# database.py
import asyncpg
from config import DATABASE_URL
import sys

pool = None

async def init_db():
    global pool
    if not DATABASE_URL:
        print("FATAL ERROR: DATABASE_URL is not found in config.py!")
        sys.exit(1)
        
    pool = await asyncpg.create_pool(
        dsn=DATABASE_URL, 
        ssl="require",
        statement_cache_size=0 # Disables statement caching for PgBouncer compatibility
    )
    print("📡 Connected to shared Supabase PostgreSQL Database Pool.")

async def get_account_by_id(account_id: str):
    """Queries your actual 'accounts' table using the UUID."""
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow('SELECT * FROM accounts WHERE id = $1::uuid', account_id)
            return dict(row) if row else None
        except Exception as e:
            print(f"Error querying accounts table: {e}")
            return None
