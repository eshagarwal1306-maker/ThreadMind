"""Memory search implementation."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Callable, Optional

from app.db import get_connection
from app.utils import cosine_similarity


class MemorySearcher:
    """Hybrid semantic and keyword-based search over memory items."""

    def __init__(self, embedding_generator: Optional[Callable[[str], Optional[list[float]]]] = None) -> None:
        self.embedding_generator = embedding_generator

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        conn = get_connection()
        rows = conn.execute("SELECT * FROM memory_items").fetchall()
        conn.close()
        row_dicts = [dict(row) for row in rows]

        if not query or not query.strip():
            return self._diverse_recent_results(row_dicts, top_k)

        if self._is_broad_summary_query(query):
            return self._diverse_summary_results(row_dicts, query, top_k)

        terms = self._tokenize(query)
        query_embedding = self.embedding_generator(query) if self.embedding_generator else None
        scored: list[tuple[float, dict[str, Any]]] = []
        rows_by_thread: dict[str, list[dict[str, Any]]] = {}
        for row_dict in row_dicts:
            rows_by_thread.setdefault(row_dict["source_thread"], []).append(row_dict)
            haystack = (row_dict.get("searchable_text") or "").lower()
            if not haystack:
                haystack = " ".join(
                    str(row_dict.get(field, "")).lower()
                    for field in ["type", "topic", "summary", "owner", "source_channel", "source_thread"]
                )
            haystack_tokens = self._tokenize(haystack)
            counts = Counter(haystack_tokens)
            lexical_score = float(sum(counts.get(term, 0) for term in terms))
            lexical_score += float(sum(1 for term in terms if term in haystack))

            semantic_score = 0.0
            embedding_json = row_dict.get("embedding_json")
            if query_embedding and embedding_json:
                try:
                    semantic_score = cosine_similarity(query_embedding, json.loads(embedding_json))
                except json.JSONDecodeError:
                    semantic_score = 0.0

            score = lexical_score + (semantic_score * 6.0)
            if score > 0.0:
                scored.append((score, row_dict))

        scored.sort(
            key=lambda item: (item[0], item[1]["confidence"], item[1]["created_at"]),
            reverse=True,
        )
        if not scored:
            return []

        selected: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        top_threads = [entry["source_thread"] for _, entry in scored[: min(3, len(scored))]]

        # Start with the strongest hit from each top thread.
        for thread_id in top_threads:
            best_in_thread = next(
                (entry for _, entry in scored if entry["source_thread"] == thread_id),
                None,
            )
            if best_in_thread and best_in_thread["memory_id"] not in seen_ids:
                selected.append(best_in_thread)
                seen_ids.add(best_in_thread["memory_id"])
            if len(selected) >= top_k:
                return selected[:top_k]

        # Expand with sibling memories from top matching threads so decisions,
        # owners, and risks from the same Slack thread can travel together.
        for thread_id in top_threads:
            siblings = sorted(
                rows_by_thread[thread_id],
                key=lambda item: (item["confidence"], item["created_at"]),
                reverse=True,
            )
            for sibling in siblings:
                if sibling["memory_id"] not in seen_ids:
                    selected.append(sibling)
                    seen_ids.add(sibling["memory_id"])
                if len(selected) >= top_k:
                    return selected[:top_k]

        # Fill any remaining slots with the rest of the scored memories.
        for _, entry in scored:
            if entry["memory_id"] not in seen_ids:
                selected.append(entry)
                seen_ids.add(entry["memory_id"])
            if len(selected) >= top_k:
                return selected[:top_k]

        return selected[:top_k]

    def _diverse_recent_results(self, rows: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        ordered = sorted(
            rows,
            key=lambda item: (item["created_at"], item["confidence"]),
            reverse=True,
        )
        return self._select_diverse(ordered, top_k)

    def _diverse_summary_results(self, rows: list[dict[str, Any]], query: str, top_k: int) -> list[dict[str, Any]]:
        ordered = sorted(
            rows,
            key=lambda item: (item["created_at"], item["confidence"]),
            reverse=True,
        )
        if any(token in query.lower() for token in ["last week", "this week", "recent", "important bits", "important", "summary"]):
            return self._select_diverse(ordered, top_k)
        return ordered[:top_k]

    def _select_diverse(self, ordered: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen_threads: set[str] = set()
        seen_types: set[str] = set()

        for row in ordered:
            if row["source_thread"] not in seen_threads and row["type"] not in seen_types:
                selected.append(row)
                seen_threads.add(row["source_thread"])
                seen_types.add(row["type"])
            if len(selected) >= top_k:
                return selected[:top_k]

        for row in ordered:
            if row["memory_id"] not in {entry["memory_id"] for entry in selected}:
                selected.append(row)
            if len(selected) >= top_k:
                return selected[:top_k]
        return selected[:top_k]

    @staticmethod
    def _is_broad_summary_query(query: str) -> bool:
        lowered = query.lower()
        markers = [
            "summary",
            "summarise",
            "summarize",
            "important bits",
            "what happened",
            "last week",
            "this week",
            "recent updates",
            "recent context",
        ]
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", text.lower())
