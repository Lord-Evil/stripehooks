"""SQLite database setup and operations."""
import aiosqlite
import json
from pathlib import Path
from typing import Optional

from .config import DB_PATH


async def init_db() -> None:
    """Initialize database schema."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS product_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                action_value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(product_id, action_type, action_value)
            );

            CREATE INDEX IF NOT EXISTS idx_product_rules_product_id
                ON product_rules(product_id);

            CREATE TABLE IF NOT EXISTS payment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id TEXT NOT NULL,
                product_name TEXT,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                payment_intent_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(payment_intent_id)
            );

            CREATE INDEX IF NOT EXISTS idx_payment_history_created_at
                ON payment_history(created_at);
            CREATE INDEX IF NOT EXISTS idx_payment_history_product_id
                ON payment_history(product_id);
        """)
        # Migration: add enabled column to product_rules
        async with db.execute("PRAGMA table_info(product_rules)") as cur:
            cols = [row[1] for row in await cur.fetchall()]
        if "enabled" not in cols:
            await db.execute("ALTER TABLE product_rules ADD COLUMN enabled INTEGER DEFAULT 1")
        await db.commit()


async def get_setting(key: str) -> Optional[str]:
    """Get a setting value by key."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    """Set a setting value."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def get_product_rules(product_id: str, include_disabled: bool = False) -> list[dict]:
    """Get rules for a product. By default only returns enabled rules (for webhook)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        where = "product_id = ?"
        if not include_disabled:
            where += " AND (enabled IS NULL OR enabled = 1)"
        async with db.execute(
            f"SELECT id, action_type, action_value FROM product_rules WHERE {where}",
            (product_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {"id": r["id"], "type": r["action_type"], "value": r["action_value"]}
                for r in rows
            ]


async def get_rules_for_product(product_id: str) -> list[dict]:
    """Get action rules for a product (convenience alias)."""
    return await get_product_rules(product_id)


async def add_product_rule(product_id: str, action_type: str, action_value: str) -> int:
    """Add a rule for a product. Returns the new rule id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO product_rules (product_id, action_type, action_value, enabled) VALUES (?, ?, ?, 1)",
            (product_id, action_type, action_value),
        )
        await db.commit()
        return cursor.lastrowid


async def delete_product_rule(rule_id: int) -> bool:
    """Delete a product rule by id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM product_rules WHERE id = ?", (rule_id,))
        await db.commit()
        return cursor.rowcount > 0


async def set_rule_enabled(rule_id: int, enabled: bool) -> bool:
    """Enable or disable a rule. Returns True if updated."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE product_rules SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, rule_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_all_product_rules() -> dict[str, list[dict]]:
    """Get all product rules grouped by product_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, product_id, action_type, action_value, enabled FROM product_rules "
            "ORDER BY product_id, id"
        ) as cursor:
            rows = await cursor.fetchall()
            result: dict[str, list[dict]] = {}
            for r in rows:
                pid = r["product_id"]
                if pid not in result:
                    result[pid] = []
                result[pid].append({
                    "id": r["id"],
                    "type": r["action_type"],
                    "value": r["action_value"],
                    "enabled": r["enabled"] is None or r["enabled"] == 1,
                })
            return result


async def insert_payment_history(
    product_id: str,
    product_name: str,
    amount: int,
    currency: str,
    payment_intent_id: str,
    created_at: int,
) -> None:
    """Record a successfully matched payment (idempotent by payment_intent_id)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO payment_history "
            "(product_id, product_name, amount, currency, payment_intent_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (product_id, product_name, amount, currency, payment_intent_id, created_at),
        )
        await db.commit()


async def get_payment_analytics(
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> list[dict]:
    """
    Get analytics grouped by product. Returns list of dicts with:
    product_id, product_name, count, total_amount, currency
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT product_id, product_name,
                   COUNT(*) as count,
                   SUM(amount) as total_amount,
                   currency
            FROM payment_history
            WHERE 1=1
        """
        params: list = []
        if start_ts is not None:
            query += " AND created_at >= ?"
            params.append(start_ts)
        if end_ts is not None:
            query += " AND created_at <= ?"
            params.append(end_ts)
        query += " GROUP BY product_id, currency ORDER BY total_amount DESC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "product_id": r["product_id"],
                    "product_name": r["product_name"] or r["product_id"],
                    "count": r["count"],
                    "total_amount": r["total_amount"],
                    "currency": r["currency"],
                }
                for r in rows
            ]
