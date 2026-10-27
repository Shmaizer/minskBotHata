import aiosqlite
DB_PATH = "listings.sqlite3"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS seen_listings (
                url TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def seen(url):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM seen_listings WHERE url = ?", (url,))
        return await cur.fetchone() is not None

async def mark_seen(url, source, title):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_listings(url, source, title) VALUES (?, ?, ?)",
            (url, source, title)
        )
        await db.commit()

async def get_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM seen_listings")
        return (await cur.fetchone())[0]
