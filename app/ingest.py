"""Ingestion pipeline for Slack memory and CSV-backed data."""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any, Optional

from openai import OpenAI

from app.db import init_db, load_csv_to_table, rebuild_database, replace_memory_items, store_raw_memory
from app.schemas import MemoryItem
from app.utils import DATA_DIR, get_env, load_env, setup_logging

logger = logging.getLogger(__name__)


EXTRACTION_PROMPT = """
Extract 0 to 4 reusable organisational memory items from this Slack-style thread.

Allowed memory item types:
- decision
- open_issue
- deadline
- owner_update
- risk
- customer_context
- supplier_context

Only extract concrete, reusable facts. Ignore banter.
Return JSON with shape:
{"items": [{"type": "...", "topic": "...", "summary": "...", "owner": null, "confidence": 0.0}]}
""".strip()


def heuristic_extract(thread: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    text = " ".join(msg["text"] for msg in thread["messages"]).lower()
    latest_ts = thread["messages"][-1]["ts"]

    def find_message(*tokens: str) -> Optional[str]:
        for message in thread["messages"]:
            lowered = message["text"].lower()
            if any(token in lowered for token in tokens):
                return message["text"]
        return None

    def add(item_type: str, topic: str, summary: str, owner: str = None, confidence: float = 0.0) -> None:
        items.append(
            MemoryItem(
                memory_id="",
                type=item_type,
                topic=topic,
                summary=summary,
                owner=owner,
                source_channel=thread["channel"],
                source_thread=thread["thread_id"],
                created_at=latest_ts,
                confidence=confidence,
            ).model_dump()
        )

    if any(token in text for token in ["decision:", "approved", "do not promise", "prioritize", "keep communication factual"]):
        decision_msg = find_message("decision:", "approved", "do not promise", "prioritize", "keep communication factual")
        if decision_msg:
            add("decision", thread["thread_id"], decision_msg, None, 0.8)
    if "owner" in text or "own" in text or "ownership change" in text:
        owner_msg = find_message("owner", "own", "ownership change")
        if owner_msg:
            add("owner_update", thread["thread_id"], owner_msg, None, 0.78)
    if any(token in text for token in ["risk", "at risk", "delay", "shortage", "conflicting", "discrepancy", "penalty", "stoppage"]):
        risk_msg = find_message("risk", "delay", "shortage", "conflicting", "discrepancy", "penalty", "stoppage")
        if risk_msg:
            add("risk", thread["thread_id"], risk_msg, None, 0.74)
    if any(token in text for token in ["deadline", "by", "eta", "ship date"]):
        deadline_msg = find_message("deadline", "eta", "ship date", "by end of day", "by tomorrow", "by 20:00")
        if deadline_msg:
            add("deadline", thread["thread_id"], deadline_msg, None, 0.7)
    if any(token in text for token in ["customer", "atlas robotics", "helios health", "northwind", "lumen", "polaris", "kestrel"]):
        customer_msg = find_message("customer", "atlas robotics", "helios health", "northwind", "lumen", "polaris", "kestrel")
        if customer_msg:
            add("customer_context", thread["thread_id"], customer_msg, None, 0.73)
    if "supplier" in text or "zeta" in text or "orion" in text:
        supplier_msg = find_message("supplier", "zeta", "orion", "nova")
        if supplier_msg:
            add("supplier_context", thread["thread_id"], supplier_msg, None, 0.76)
    if any(token in text for token in ["blocker", "unresolved", "validate before we escalate", "no proof", "not confirmed"]):
        issue_msg = find_message("blocker", "unresolved", "validate before we escalate", "no proof", "not confirmed")
        if issue_msg:
            add("open_issue", thread["thread_id"], issue_msg, None, 0.7)

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item["type"], item["summary"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped[:4]


def llm_extract(client: OpenAI, model: str, thread: dict[str, Any]) -> list[dict[str, Any]]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": json.dumps(thread)},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    payload = json.loads(content)
    raw_items = payload.get("items", [])
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        item = MemoryItem(
            memory_id=f"mem_{thread['thread_id']}_{index + 1}",
            type=raw["type"],
            topic=raw["topic"],
            summary=raw["summary"],
            owner=raw.get("owner"),
            source_channel=thread["channel"],
            source_thread=thread["thread_id"],
            created_at=thread["messages"][-1]["ts"],
            confidence=raw["confidence"],
        ).model_dump()
        items.append(item)
    return items


def build_searchable_text(item: dict[str, Any]) -> str:
    owner = item.get("owner") or ""
    return " | ".join(
        [
            item["type"],
            item["topic"],
            item["summary"],
            owner,
            item["source_channel"],
            item["source_thread"],
        ]
    )


def generate_memory_embeddings(client: OpenAI, items: list[dict[str, Any]], model: str) -> list[Optional[str]]:
    texts = [item["searchable_text"] for item in items]
    response = client.embeddings.create(model=model, input=texts)
    return [json.dumps(entry.embedding) for entry in response.data]


def ingest(rebuild: bool = False) -> None:
    load_env()
    setup_logging()
    if rebuild:
        rebuild_database()
    init_db()

    api_key = get_env("OPENAI_API_KEY")
    model = get_env("THREADMIND_MODEL", "gpt-4.1-mini")
    embedding_model = get_env("THREADMIND_EMBEDDING_MODEL", "text-embedding-3-small")
    client = OpenAI(api_key=api_key) if api_key else None

    threads = json.loads((DATA_DIR / "slack_threads.json").read_text(encoding="utf-8"))
    memory_items: list[dict[str, Any]] = []

    for thread in threads:
        if client is not None:
            try:
                extracted = llm_extract(client, model, thread)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM extraction failed for %s, using heuristic fallback: %s", thread["thread_id"], exc)
                extracted = heuristic_extract(thread)
        else:
            extracted = heuristic_extract(thread)

        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(extracted):
            item["memory_id"] = item.get("memory_id") or f"mem_{thread['thread_id']}_{index + 1}"
            item["source_channel"] = thread["channel"]
            item["source_thread"] = thread["thread_id"]
            item["created_at"] = thread["messages"][-1]["ts"]
            normalized_item = MemoryItem.model_validate(item).model_dump()
            normalized_item["searchable_text"] = build_searchable_text(normalized_item)
            normalized.append(normalized_item)
        store_raw_memory(thread["thread_id"], normalized)
        memory_items.extend(normalized)
        logger.info("Thread %s -> %s memory items", thread["thread_id"], len(normalized))

    if client is not None and memory_items:
        try:
            embeddings = generate_memory_embeddings(client, memory_items, embedding_model)
            for item, embedding_json in zip(memory_items, embeddings):
                item["embedding_json"] = embedding_json
            logger.info("Generated semantic embeddings for %s memory items", len(memory_items))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding generation failed, continuing with lexical fallback: %s", exc)

    replace_memory_items(memory_items)
    for table in ["orders", "inventory", "suppliers", "customers"]:
        load_csv_to_table(DATA_DIR / f"{table}.csv", table)
        logger.info("Loaded %s.csv into %s", table, table)

    logger.info("Ingestion complete. Memory items stored: %s", len(memory_items))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the SQLite database from scratch.")
    args = parser.parse_args()
    ingest(rebuild=args.rebuild)


if __name__ == "__main__":
    main()
