"""Structured data querying."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Optional

from app.db import get_connection

logger = logging.getLogger(__name__)


SCHEMA_TEXT = """
Tables:
- orders(order_id, customer_id, sku, quantity, due_date, status, margin_pct, priority_level)
- inventory(sku, on_hand, incoming_qty, reserved_qty, warehouse)
- suppliers(supplier_id, supplier_name, sku, expected_ship_date, delay_risk, status)
- customers(customer_id, customer_name, strategic_tier, region)
""".strip()


class DataQueryEngine:
    """Runs safe, read-only SQL over local operational data."""

    def __init__(self, sql_generator: Optional[Any] = None) -> None:
        self.sql_generator = sql_generator

    def query(self, question: str) -> dict[str, Any]:
        sql = self._generate_sql(question)
        rows = self._run_safe_sql(sql)
        return {"sql": sql, "rows": rows}

    def _generate_sql(self, question: str) -> str:
        if self.sql_generator is not None:
            sql = self.sql_generator(question, SCHEMA_TEXT)
            if sql:
                return sql
        return self._heuristic_sql(question)

    def _heuristic_sql(self, question: str) -> str:
        order_match = re.search(r"order\s+(\d+)", question, re.IGNORECASE)
        supplier_match = re.search(r"supplier\s+([A-Za-z]+)|zeta|orion|nova", question, re.IGNORECASE)
        customer_match = re.search(r"atlas robotics|helios health|northwind|lumen|polaris|kestrel", question, re.IGNORECASE)
        sku_match = re.search(r"sigma valves|valves|bearings|brg-100|val-220", question, re.IGNORECASE)

        if order_match:
            order_id = order_match.group(1)
            return f"""
            SELECT
              'orders:' || o.order_id AS data_ref,
              o.order_id, o.customer_id, c.customer_name, o.sku, o.quantity,
              o.due_date, o.status, o.margin_pct, o.priority_level,
              COALESCE(SUM(i.on_hand), 0) AS total_on_hand,
              COALESCE(SUM(i.incoming_qty), 0) AS total_incoming,
              COALESCE(SUM(i.reserved_qty), 0) AS total_reserved,
              s.supplier_id, s.supplier_name, s.expected_ship_date, s.delay_risk, s.status AS supplier_status
            FROM orders o
            LEFT JOIN customers c ON c.customer_id = o.customer_id
            LEFT JOIN inventory i ON i.sku = o.sku
            LEFT JOIN suppliers s ON s.sku = o.sku
            WHERE o.order_id = {int(order_id)}
            GROUP BY o.order_id, c.customer_name, s.supplier_id
            """

        if supplier_match:
            name = supplier_match.group(0)
            return f"""
            SELECT 'suppliers:' || supplier_id AS data_ref, *
            FROM suppliers
            WHERE lower(supplier_name) LIKE '%{name.lower()}%'
            """

        if customer_match:
            name = customer_match.group(0).lower()
            return f"""
            SELECT 'customers:' || c.customer_id AS data_ref, c.*, o.order_id, o.status, o.priority_level
            FROM customers c
            LEFT JOIN orders o ON o.customer_id = c.customer_id
            WHERE lower(c.customer_name) LIKE '%{name}%'
            ORDER BY o.order_id
            """

        if sku_match:
            sku = "VAL-220" if "val" in sku_match.group(0).lower() or "sigma" in sku_match.group(0).lower() else "BRG-100"
            return f"""
            SELECT 'inventory:' || sku || ':' || warehouse AS data_ref, *
            FROM inventory
            WHERE sku = '{sku}'
            """

        return """
        SELECT 'orders:' || order_id AS data_ref, order_id, customer_id, sku, due_date, status
        FROM orders
        ORDER BY due_date
        LIMIT 5
        """

    def _run_safe_sql(self, sql: str) -> list[dict[str, Any]]:
        normalized = sql.strip().lower()
        if not normalized.startswith("select"):
            raise ValueError("Only SELECT queries are allowed.")
        blocked = ["insert ", "update ", "delete ", "drop ", "alter ", "attach ", "pragma "]
        if any(token in normalized for token in blocked):
            raise ValueError("Blocked SQL detected.")

        conn = get_connection()
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error as exc:
            logger.warning("SQL execution failed: %s", exc)
            conn.close()
            return [{"error": str(exc), "sql": sql}]
        conn.close()
        return [dict(row) for row in rows]


def parse_sql_candidate(text: str) -> Optional[str]:
    """Extract SQL from a fenced or plain model response."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*", "", cleaned).strip()
        cleaned = cleaned.removesuffix("```").strip()
    if cleaned.lower().startswith("select"):
        return cleaned
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    sql = payload.get("sql")
    return sql if isinstance(sql, str) else None
