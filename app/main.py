"""Interactive CLI for ThreadMind."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.agent import ThreadMindAgent
from app.ingest import ingest
from app.prompts import CONSERVATIVE_PROMPT, PROACTIVE_PROMPT
from app.utils import DB_PATH, ensure_directories, load_env, setup_logging


MODE_LABELS = {
    "answer": "Answer",
    "ask_clarification": "Need clarification",
    "out_of_scope": "Out of scope",
    "create_action": "Action created",
}


def _format_sources(citations: dict[str, list[str]]) -> str:
    memory_ids = citations.get("memory_ids", [])
    data_refs = citations.get("data_refs", [])
    if memory_ids and data_refs:
        return "recent team discussions and business records"
    if memory_ids:
        return "recent team discussions"
    if data_refs:
        return "business records"
    return "direct reasoning from the current request"


def _tool_focus(call: dict[str, Any]) -> str:
    tool_name = call["tool_name"]
    arguments = call.get("arguments", {})
    if tool_name == "search_memory":
        query = arguments.get("query", "")
        return query if query else "the most relevant recent discussions"
    if tool_name == "query_structured_data":
        return arguments.get("question", "structured business records")
    if tool_name == "create_action":
        title = arguments.get("title", "follow-up action")
        priority = arguments.get("priority", "unknown")
        return f"{title} ({priority})"
    return "internal lookup"


def render_result(result: Any, debug: bool = False) -> str:
    final = result.final
    lines = [
        "",
        f"{MODE_LABELS.get(final.mode.value, final.mode.value)}",
        f"{final.answer}",
    ]
    if final.next_steps:
        lines.append("")
        lines.append("Recommended next steps:")
        for step in final.next_steps[:5]:
            lines.append(f"- {step}")
    lines.append("")
    lines.append(f"Why: {final.reasoning_summary}")
    if final.created_action_id:
        lines.append("I also logged a follow-up action so the team can track it.")
    lines.append(f"Based on: {_format_sources(final.citations)}")
    if result.tool_calls:
        lines.append("How I checked:")
        seen = []
        for call in result.tool_calls:
            label = {
                "search_memory": "I reviewed the most relevant team discussions",
                "query_structured_data": "I checked the latest business records",
                "create_action": "I logged a trackable follow-up",
            }.get(call["tool_name"], call["tool_name"])
            focus = _tool_focus(call)
            if call["tool_name"] == "search_memory":
                detail = f"- {label} about: {focus}"
            elif call["tool_name"] == "query_structured_data":
                detail = f"- {label} for: {focus}"
            elif call["tool_name"] == "create_action":
                detail = f"- {label}: {focus}"
            else:
                detail = f"- {label}"
            if detail not in seen:
                seen.append(detail)
                lines.append(detail)
        if debug:
            lines.append("")
            lines.append("Tool call details:")
            for index, call in enumerate(result.tool_calls, start=1):
                lines.append(f"{index}. {call['tool_name']}")
                lines.append(f"   input: {json.dumps(call.get('arguments', {}), ensure_ascii=True)}")
                lines.append(f"   result: {json.dumps(call.get('result_preview', {}), ensure_ascii=True)}")
    return "\n".join(lines)


def render_progress(event: dict[str, Any], debug: bool = False) -> None:
    if event["status"] == "started":
        print(f"[{event['phase']}] {event['message']}")
        sys.stdout.flush()
        return
    if debug and event["phase"] == "tool_call":
        print(f"[{event['phase']}] {event['message']}")
        if "tool_name" in event:
            print(f"  tool: {event['tool_name']}")
        if "arguments" in event:
            print(f"  input: {json.dumps(event['arguments'], ensure_ascii=True)}")
        if "result_preview" in event:
            print(f"  result: {json.dumps(event['result_preview'], ensure_ascii=True)}")
        sys.stdout.flush()
        return
    if event["status"] == "completed" and event["phase"] != "finalizing":
        print(f"[{event['phase']}] {event['message']}")
        sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompt-style",
        choices=["proactive", "conservative"],
        default="conservative",
        help="System prompt variant to run.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show full backend tool inputs and results.",
    )
    args = parser.parse_args()

    load_env()
    setup_logging()
    ensure_directories()

    if not DB_PATH.exists():
        print("Database not found. Running ingestion first.")
        ingest(rebuild=True)

    system_prompt = PROACTIVE_PROMPT if args.prompt_style == "proactive" else CONSERVATIVE_PROMPT
    try:
        agent = ThreadMindAgent(system_prompt=system_prompt)
    except RuntimeError as exc:
        print(f"Startup error: {exc}")
        print("Set OPENAI_API_KEY in .env, then run `make run` again.")
        return

    print("ThreadMind interactive CLI. Type 'exit' to quit.")
    print(f"Mode: {'debug' if args.debug else 'clean'}")
    conversation_history: list[dict[str, str]] = []
    while True:
        user_prompt = input("\n> ").strip()
        if user_prompt.lower() in {"exit", "quit"}:
            break
        if not user_prompt:
            continue
        result = agent.run(
            user_prompt,
            conversation_history=conversation_history,
            progress_callback=lambda event: render_progress(event, debug=args.debug),
        )
        conversation_history.append({"role": "user", "content": user_prompt})
        conversation_history.append({"role": "assistant", "content": result.final.answer})
        conversation_history = conversation_history[-12:]
        print(render_result(result, debug=args.debug))


if __name__ == "__main__":
    main()
