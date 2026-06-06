"""LLM-driven agent runtime."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from openai import APIConnectionError, APITimeoutError, InternalServerError, OpenAI, RateLimitError
from pydantic import ValidationError

from app.data_query import DataQueryEngine, SCHEMA_TEXT, parse_sql_candidate
from app.memory_search import MemorySearcher
from app.prompts import FINAL_RESPONSE_INSTRUCTION
from app.schemas import FinalResponse
from app.tools import ToolRegistry, json_dumps
from app.utils import get_env

logger = logging.getLogger(__name__)


@dataclass
class AgentRunResult:
    final: FinalResponse
    tool_calls: list[dict[str, Any]]
    latency_seconds: float
    raw_final_text: str


def _build_client() -> OpenAI:
    api_key = get_env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required to run ThreadMind.")
    return OpenAI(api_key=api_key)


class ThreadMindAgent:
    """Multi-step tool-calling agent."""

    def __init__(self, system_prompt: str, model: Optional[str] = None) -> None:
        self.client = _build_client()
        self.model = model or get_env("THREADMIND_MODEL", "gpt-4.1-mini")
        self.embedding_model = get_env("THREADMIND_EMBEDDING_MODEL", "text-embedding-3-small")
        self.system_prompt = system_prompt
        self.max_completion_tokens = int(get_env("THREADMIND_MAX_COMPLETION_TOKENS", "220") or "220")
        self.max_sql_completion_tokens = int(get_env("THREADMIND_MAX_SQL_COMPLETION_TOKENS", "120") or "120")
        self.max_final_completion_tokens = int(get_env("THREADMIND_MAX_FINAL_COMPLETION_TOKENS", "220") or "220")
        self.data_query_engine = DataQueryEngine(sql_generator=self._generate_sql_with_model)
        self.memory_searcher = MemorySearcher(embedding_generator=self._generate_query_embedding)
        self.tools = ToolRegistry(self.data_query_engine, self.memory_searcher)

    def run(
        self,
        user_prompt: str,
        max_rounds: int = 6,
        conversation_history: Optional[list[dict[str, str]]] = None,
        progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> AgentRunResult:
        start = time.perf_counter()
        self._emit_progress(progress_callback, "thinking", "started", "Thinking through your request.")
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
        ]
        if conversation_history:
            messages.extend(conversation_history[-8:])
        messages.append({"role": "user", "content": user_prompt})
        tool_logs: list[dict[str, Any]] = []

        for _ in range(max_rounds):
            response = self._chat_completion_with_retry(
                model=self.model,
                messages=messages,
                tools=self.tools.tool_specs,
                tool_choice="auto",
                temperature=0,
                max_completion_tokens=self.max_completion_tokens,
            )
            message = response.choices[0].message
            if not message.tool_calls:
                messages.append({"role": "assistant", "content": message.content or ""})
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.function.name,
                                "arguments": tool_call.function.arguments,
                            },
                        }
                        for tool_call in message.tool_calls
                    ],
                }
            )
            for tool_call in message.tool_calls:
                self._emit_progress(
                    progress_callback,
                    "tool_call",
                    "started",
                    self._tool_progress_message(tool_call.function.name),
                    {
                        "tool_name": tool_call.function.name,
                        "arguments": json.loads(tool_call.function.arguments),
                    },
                )
                result = self.tools.call_tool(
                    tool_call.function.name,
                    tool_call.function.arguments,
                )
                tool_logs.append(
                    {
                        "tool_name": tool_call.function.name,
                        "arguments": json.loads(tool_call.function.arguments),
                        "result_preview": result,
                    }
                )
                self._emit_progress(
                    progress_callback,
                    "tool_call",
                    "completed",
                    self._tool_progress_done_message(tool_call.function.name, result),
                    {
                        "tool_name": tool_call.function.name,
                        "arguments": json.loads(tool_call.function.arguments),
                        "result_preview": result,
                    },
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json_dumps(result),
                    }
                )
        self._emit_progress(progress_callback, "finalizing", "started", "Pulling everything together.")
        final = self._finalize(messages, tool_logs, user_prompt)
        self._emit_progress(progress_callback, "finalizing", "completed", "Answer ready.")
        latency = time.perf_counter() - start
        return AgentRunResult(
            final=final,
            tool_calls=tool_logs,
            latency_seconds=latency,
            raw_final_text=messages[-1]["content"] if messages else "",
        )

    def _finalize(
        self,
        messages: list[dict[str, Any]],
        tool_logs: list[dict[str, Any]],
        user_prompt: str,
    ) -> FinalResponse:
        tool_summary = self._build_tool_summary(tool_logs)
        final_messages = messages + [
            {
                "role": "system",
                "content": (
                    FINAL_RESPONSE_INSTRUCTION
                    + "\nUse only evidence already in the conversation and tool results."
                    + "\nKeep the JSON compact."
                    + f"\nOriginal user prompt: {user_prompt}"
                    + f"\nTool evidence summary:\n{tool_summary}"
                ),
            }
        ]

        for attempt in range(2):
            response = self._chat_completion_with_retry(
                model=self.model,
                messages=final_messages,
                temperature=0,
                response_format={"type": "json_object"},
                max_completion_tokens=self.max_final_completion_tokens,
            )
            content = response.choices[0].message.content or "{}"
            try:
                final = self._parse_final_response(content, tool_logs)
                final.used_tools = [call["tool_name"] for call in tool_logs]
                if final.created_action_id is None:
                    for call in tool_logs:
                        if call["tool_name"] == "create_action":
                            action_id = call["result_preview"].get("action_id")
                            if action_id:
                                final.created_action_id = action_id
                if final.mode.value == "create_action" and final.created_action_id is None:
                    raise RuntimeError("Model chose create_action mode without actually calling create_action.")
                return final
            except (json.JSONDecodeError, ValidationError) as exc:
                logger.warning("Final response validation failed on attempt %s: %s", attempt + 1, exc)
                final_messages.append(
                    {
                        "role": "system",
                        "content": "Your previous output did not match the required JSON schema. Return smaller valid JSON only.",
                    }
                )
            except RuntimeError as exc:
                logger.warning("Final response runtime validation failed on attempt %s: %s", attempt + 1, exc)
                final_messages.append(
                    {
                        "role": "system",
                        "content": "Your previous output was incomplete or invalid. Return compact valid JSON only.",
                    }
                )
        return self._deterministic_final_fallback(tool_logs, user_prompt)

    def _generate_sql_with_model(self, question: str, schema_text: str) -> Optional[str]:
        prompt = (
            "Generate a single SQLite SELECT query that answers the question using the schema.\n"
            "Rules: only SELECT, no markdown explanation, no comments, no DDL, no DML, keep it minimal.\n"
            "Return JSON: {\"sql\": \"...\"}.\n"
            f"Schema:\n{schema_text}\n"
            f"Question: {question}"
        )
        response = self._chat_completion_with_retry(
            model=self.model,
            messages=[
                {"role": "system", "content": "You write safe SQLite SELECT queries."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            max_completion_tokens=self.max_sql_completion_tokens,
        )
        content = response.choices[0].message.content or ""
        return parse_sql_candidate(content)

    def _generate_query_embedding(self, text: str) -> Optional[list[float]]:
        response = self._embedding_with_retry(
            model=self.embedding_model,
            input=[text],
        )
        if not response.data:
            return None
        return response.data[0].embedding

    def _chat_completion_with_retry(self, **kwargs: Any) -> Any:
        rate_delay = 60.0
        conn_delay = 1.0
        for attempt in range(6):
            try:
                return self.client.chat.completions.create(**kwargs)
            except RateLimitError as exc:
                if attempt == 5:
                    raise
                logger.warning("Chat completion rate-limit retry %s (waiting %.0fs): %s", attempt + 1, rate_delay, exc)
                time.sleep(rate_delay)
                rate_delay = min(rate_delay * 1.5, 120.0)
            except (APIConnectionError, APITimeoutError, InternalServerError) as exc:
                if attempt == 5:
                    raise
                logger.warning("Chat completion retry %s after error: %s", attempt + 1, exc)
                time.sleep(conn_delay)
                conn_delay = min(conn_delay * 2.0, 16.0)
        raise RuntimeError("Unreachable retry state for chat completions.")

    def _embedding_with_retry(self, **kwargs: Any) -> Any:
        delay_seconds = 1.0
        for attempt in range(4):
            try:
                return self.client.embeddings.create(**kwargs)
            except (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError) as exc:
                if attempt == 3:
                    raise
                logger.warning("Embedding retry %s after error: %s", attempt + 1, exc)
                time.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2.0, 6.0)
        raise RuntimeError("Unreachable retry state for embeddings.")

    def _parse_final_response(
        self,
        content: str,
        tool_logs: list[dict[str, Any]],
    ) -> FinalResponse:
        try:
            payload = json.loads(content)
            return FinalResponse.model_validate(payload)
        except (json.JSONDecodeError, ValidationError):
            repaired = self._repair_final_json(content, tool_logs)
            payload = json.loads(repaired)
            return FinalResponse.model_validate(payload)

    def _repair_final_json(self, broken_content: str, tool_logs: list[dict[str, Any]]) -> str:
        repair_messages = [
            {
                "role": "system",
                "content": (
                    "Repair the following broken JSON into valid compact JSON that matches the required schema. "
                    "Do not add markdown. Return compact JSON only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"{FINAL_RESPONSE_INSTRUCTION}\n"
                    f"Tools actually used: {[call['tool_name'] for call in tool_logs]}\n"
                    f"Broken JSON:\n{broken_content}"
                ),
            },
        ]
        response = self._chat_completion_with_retry(
            model=self.model,
            messages=repair_messages,
            temperature=0,
            response_format={"type": "json_object"},
            max_completion_tokens=self.max_final_completion_tokens,
        )
        return response.choices[0].message.content or "{}"

    def _build_tool_summary(self, tool_logs: list[dict[str, Any]]) -> str:
        if not tool_logs:
            return "No tools were used."
        parts: list[str] = []
        for call in tool_logs:
            if call["tool_name"] == "search_memory":
                results = call["result_preview"].get("results", [])
                summaries = [item.get("summary", "") for item in results[:4]]
                parts.append("Memory: " + " | ".join(summary for summary in summaries if summary))
            elif call["tool_name"] == "query_structured_data":
                rows = call["result_preview"].get("rows", [])
                compact_rows = [json.dumps(row, ensure_ascii=True) for row in rows[:3]]
                parts.append("Data: " + " | ".join(compact_rows))
            elif call["tool_name"] == "create_action":
                action_id = call["result_preview"].get("action_id", "created action")
                title = call["result_preview"].get("title", "")
                parts.append(f"Action: {action_id} {title}".strip())
        return "\n".join(parts)

    def _deterministic_final_fallback(
        self,
        tool_logs: list[dict[str, Any]],
        user_prompt: str,
    ) -> FinalResponse:
        memory_ids: list[str] = []
        data_refs: list[str] = []
        answer_parts: list[str] = []
        next_steps: list[str] = []

        for call in tool_logs:
            if call["tool_name"] == "search_memory":
                results = call["result_preview"].get("results", [])
                for item in results[:5]:
                    if item.get("memory_id"):
                        memory_ids.append(item["memory_id"])
                    if item.get("summary"):
                        answer_parts.append(item["summary"])
            elif call["tool_name"] == "query_structured_data":
                rows = call["result_preview"].get("rows", [])
                for row in rows[:3]:
                    if row.get("data_ref"):
                        data_refs.append(row["data_ref"])
            elif call["tool_name"] == "create_action":
                action_id = call["result_preview"].get("action_id")
                return FinalResponse(
                    mode="create_action",
                    answer="I logged the follow-up action based on the evidence I found.",
                    reasoning_summary="The action was created from the gathered discussion context and business records.",
                    used_tools=[entry["tool_name"] for entry in tool_logs],
                    citations={"memory_ids": memory_ids[:5], "data_refs": data_refs[:5]},
                    next_steps=[],
                    created_action_id=action_id,
                )

        lowered = user_prompt.lower()
        if "summary" in lowered or "summaris" in lowered or "important bits" in lowered or "what changed" in lowered:
            next_steps = [
                "Review the open risks first.",
                "Follow up on any unresolved owner actions.",
                "Check whether the latest operational records still match the discussion context.",
            ]

        if answer_parts:
            answer = "Here are the most relevant updates: " + " ".join(answer_parts[:4])
        else:
            answer = "I could not assemble a fully structured answer, but I did gather relevant context from the available tools."

        return FinalResponse(
            mode="answer",
            answer=answer,
            reasoning_summary="This fallback answer was assembled from the tool results after the structured final response step failed.",
            used_tools=[entry["tool_name"] for entry in tool_logs],
            citations={"memory_ids": memory_ids[:5], "data_refs": data_refs[:5]},
            next_steps=next_steps[:5],
            created_action_id=None,
        )

    @staticmethod
    def _emit_progress(
        callback: Optional[Callable[[dict[str, Any]], None]],
        phase: str,
        status: str,
        message: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        if callback is None:
            return
        event = {
            "phase": phase,
            "status": status,
            "message": message,
        }
        if payload:
            event.update(payload)
        callback(event)

    @staticmethod
    def _tool_progress_message(tool_name: str) -> str:
        return {
            "search_memory": "Searching through recent team discussions.",
            "query_structured_data": "Checking the latest business records.",
            "create_action": "Saving a tracked follow-up.",
        }.get(tool_name, "Using an internal tool.")

    @staticmethod
    def _tool_progress_done_message(tool_name: str, result: dict[str, Any]) -> str:
        if tool_name == "search_memory":
            count = len(result.get("results", []))
            return f"Found {count} relevant updates."
        if tool_name == "query_structured_data":
            count = len(result.get("rows", []))
            return f"Reviewed {count} matching business records."
        if tool_name == "create_action":
            action_id = result.get("action_id", "new action")
            return f"Saved tracked follow-up {action_id}."
        return "Tool step completed."
