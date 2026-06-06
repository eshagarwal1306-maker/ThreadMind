"""Tool registrations and dispatch."""

from __future__ import annotations

import json
from typing import Any

from app.action_store import ActionStore
from app.data_query import DataQueryEngine
from app.memory_search import MemorySearcher
from app.schemas import CreateActionArgs, QueryStructuredDataArgs, SearchMemoryArgs


class ToolRegistry:
    """Exposes the three ThreadMind tools."""

    def __init__(
        self,
        data_query_engine: DataQueryEngine,
        memory_searcher: MemorySearcher,
    ) -> None:
        self.memory = memory_searcher
        self.data = data_query_engine
        self.actions = ActionStore()

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "search_memory",
                    "description": "Search company memory from Slack-style discussions, meeting summaries, decisions, risks, blockers, ownership changes, and weekly updates.",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                        "required": ["query", "top_k"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "query_structured_data",
                    "description": "Check current business records such as orders, inventory, suppliers, and customers to verify operational facts.",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_action",
                    "description": "Create and save a durable follow-up action when the evidence is strong enough to justify a tracked next step.",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action_type": {
                                "type": "string",
                                "enum": ["follow_up", "escalation", "discrepancy_alert", "decision_record"],
                            },
                            "title": {"type": "string"},
                            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
                            "reason": {"type": "string"},
                            "evidence": {
                                "type": "object",
                                "properties": {
                                    "memory_ids": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "data_refs": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "rationale": {"type": "string"},
                                },
                                "required": ["memory_ids", "data_refs", "rationale"],
                                "additionalProperties": False,
                            },
                            "owner": {"type": ["string", "null"]},
                            "related_entity": {"type": ["string", "null"]},
                        },
                        "required": [
                            "action_type",
                            "title",
                            "priority",
                            "reason",
                            "evidence",
                            "owner",
                            "related_entity",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def call_tool(self, name: str, arguments_json: str) -> dict[str, Any]:
        if name == "search_memory":
            args = SearchMemoryArgs.model_validate_json(arguments_json)
            return {"results": self.memory.search(args.query, args.top_k)}
        if name == "query_structured_data":
            args = QueryStructuredDataArgs.model_validate_json(arguments_json)
            return self.data.query(args.question)
        if name == "create_action":
            args = CreateActionArgs.model_validate_json(arguments_json)
            return self.actions.create(args)
        raise ValueError(f"Unknown tool: {name}")


def json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True)
