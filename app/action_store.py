"""Durable action persistence."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from app.db import get_connection
from app.schemas import ActionRecord, CreateActionArgs, utc_now_iso
from app.utils import OUTPUTS_DIR, append_jsonl


class ActionStore:
    """Persists actions to SQLite and JSONL."""

    def create(self, args: CreateActionArgs) -> dict[str, Any]:
        record = ActionRecord(
            action_id=f"act_{uuid4().hex[:10]}",
            created_at=utc_now_iso(),
            action_type=args.action_type,
            title=args.title,
            priority=args.priority,
            owner=args.owner,
            related_entity=args.related_entity,
            reason=args.reason,
            evidence=args.evidence,
            status="open",
        )
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO actions (
                action_id, created_at, action_type, title, priority,
                owner, related_entity, reason, evidence, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.action_id,
                record.created_at,
                record.action_type.value,
                record.title,
                record.priority.value,
                record.owner,
                record.related_entity,
                record.reason,
                json.dumps(record.evidence),
                record.status,
            ),
        )
        conn.commit()
        conn.close()
        append_jsonl(OUTPUTS_DIR / "actions.jsonl", record.model_dump())
        return record.model_dump()
