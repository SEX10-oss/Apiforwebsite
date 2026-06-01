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
        statement_cache_size=0
    )
    print("📡 Connected to shared PostgreSQL Database Pool.")

async def create_account_creation_job(user_psid: str, email: str, password: str, mod_id: int, lang: str = 'en') -> int:
    async with pool.acquire() as conn:
        query = """
            INSERT INTO creation_jobs (user_psid, email, password, mod_id, status, lang) 
            VALUES ($1, $2, $3, $4, 'processing', $5) RETURNING job_id
        """
        return await conn.fetchval(query, user_psid, email, password, mod_id, lang)

async def get_mod_by_id(mod_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow('SELECT * FROM mods WHERE id = $1', mod_id)
        return dict(row) if row else None

async def get_mods_by_price(price: float):
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT * FROM mods WHERE price BETWEEN $1 AND $2', price - 0.01, price + 0.01)
        return [dict(row) for row in rows]

async def add_reference(ref: str, user_id: str, mod_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO "references" (ref_number, user_id, mod_id, claims_max) 
            VALUES ($1, $2, $3, 1) ON CONFLICT (ref_number) DO NOTHING
        """, ref, user_id, mod_id)
