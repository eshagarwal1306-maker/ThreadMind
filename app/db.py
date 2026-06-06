"""SQLite helpers for ThreadMind."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from app.utils import DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rebuild_database() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()


def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_items (
            memory_id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            topic TEXT NOT NULL,
            summary TEXT NOT NULL,
            owner TEXT,
            source_channel TEXT NOT NULL,
            source_thread TEXT NOT NULL,
            created_at TEXT NOT NULL,
            confidence REAL NOT NULL,
            searchable_text TEXT NOT NULL DEFAULT '',
            embedding_json TEXT
        );

        CREATE TABLE IF NOT EXISTS raw_memory_debug (
            thread_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY,
            customer_id TEXT NOT NULL,
            sku TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            due_date TEXT NOT NULL,
            status TEXT NOT NULL,
            margin_pct REAL NOT NULL,
            priority_level TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS inventory (
            inventory_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            on_hand INTEGER NOT NULL,
            incoming_qty INTEGER NOT NULL,
            reserved_qty INTEGER NOT NULL,
            warehouse TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS suppliers (
            supplier_id TEXT PRIMARY KEY,
            supplier_name TEXT NOT NULL,
            sku TEXT NOT NULL,
            expected_ship_date TEXT NOT NULL,
            delay_risk TEXT NOT NULL,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS customers (
            customer_id TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            strategic_tier TEXT NOT NULL,
            region TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS actions (
            action_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            action_type TEXT NOT NULL,
            title TEXT NOT NULL,
            priority TEXT NOT NULL,
            owner TEXT,
            related_entity TEXT,
            reason TEXT NOT NULL,
            evidence TEXT NOT NULL,
            status TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


def replace_memory_items(items: Iterable[dict]) -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM memory_items")
    cur.executemany(
        """
        INSERT INTO memory_items (
            memory_id, type, topic, summary, owner, source_channel,
            source_thread, created_at, confidence, searchable_text, embedding_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item["memory_id"],
                item["type"],
                item["topic"],
                item["summary"],
                item.get("owner"),
                item["source_channel"],
                item["source_thread"],
                item["created_at"],
                item["confidence"],
                item.get("searchable_text", ""),
                item.get("embedding_json"),
            )
            for item in items
        ],
    )
    conn.commit()
    conn.close()


def store_raw_memory(thread_id: str, payload: list[dict]) -> None:
    conn = get_connection()
    conn.execute(
        "REPLACE INTO raw_memory_debug (thread_id, payload) VALUES (?, ?)",
        (thread_id, json.dumps(payload)),
    )
    conn.commit()
    conn.close()


def load_csv_to_table(csv_path: Path, table_name: str) -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {table_name}")
    with csv_path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    if not rows:
        conn.commit()
        conn.close()
        return
    columns = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"
    cur.executemany(sql, [[row[col] for col in columns] for row in rows])
    conn.commit()
    conn.close()
