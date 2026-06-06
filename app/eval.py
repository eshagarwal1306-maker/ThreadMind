"""Offline benchmark runner for ThreadMind."""

from __future__ import annotations

import csv
import json
import logging
import argparse
from statistics import mean
import time
from typing import Any

from app.agent import ThreadMindAgent
from app.prompts import CONSERVATIVE_PROMPT, PROACTIVE_PROMPT
from app.schemas import EvalPrompt
from app.utils import DATA_DIR, OUTPUTS_DIR, dump_json, ensure_directories, get_env, load_env, setup_logging

logger = logging.getLogger(__name__)


def load_benchmark() -> list[EvalPrompt]:
    raw = json.loads((DATA_DIR / "eval_prompts.json").read_text(encoding="utf-8"))
    return [EvalPrompt.model_validate(item) for item in raw]


def safe_mean(values: list[float], default: float = 0.0) -> float:
    return round(mean(values), 3) if values else default


def summarize_results(variant: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "variant": variant,
        "failed_runs": sum(1 for row in results if row["actual_mode"] == "error"),
        "mode_accuracy": safe_mean([1.0 if row["mode_correct"] else 0.0 for row in results]),
        "tool_selection_accuracy": safe_mean([1.0 if row["tool_accuracy"] else 0.0 for row in results]),
        "action_precision": safe_mean(
            [
                1.0 if row["action_correct"] else 0.0
                for row in results
                if row["expected_mode"] == "create_action" or row["created_action_id"]
            ],
            default=1.0,
        ),
        "over_action_rate": safe_mean([1.0 if row["over_action"] else 0.0 for row in results]),
        "clarification_rate_on_ambiguous": safe_mean(
            [1.0 if row["clarification_hit"] else 0.0 for row in results if row["category"] == "ambiguous"]
        ),
        "out_of_scope_precision": safe_mean(
            [1.0 if row["out_of_scope_hit"] else 0.0 for row in results if row["category"] == "out_of_scope"]
        ),
        "happy_path_mode_accuracy": safe_mean(
            [1.0 if row["mode_correct"] else 0.0 for row in results if row["category"] == "happy_path"]
        ),
        "ambiguous_mode_accuracy": safe_mean(
            [1.0 if row["mode_correct"] else 0.0 for row in results if row["category"] == "ambiguous"]
        ),
        "out_of_scope_mode_accuracy": safe_mean(
            [1.0 if row["mode_correct"] else 0.0 for row in results if row["category"] == "out_of_scope"]
        ),
        "average_latency_seconds": safe_mean([row["latency_seconds"] for row in results]),
        "results": results,
    }


def load_existing_payload() -> dict[str, Any] | None:
    path = OUTPUTS_DIR / "eval_results.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def merge_summaries(existing: dict[str, Any] | None, new_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged_rows: dict[tuple[str, str], dict[str, Any]] = {}
    if existing:
        for summary in existing.get("summaries", []):
            for row in summary.get("results", []):
                merged_rows[(row["variant"], row["prompt_id"])] = row
    for summary in new_summaries:
        for row in summary.get("results", []):
            merged_rows[(row["variant"], row["prompt_id"])] = row

    by_variant: dict[str, list[dict[str, Any]]] = {}
    for (_, _), row in merged_rows.items():
        by_variant.setdefault(row["variant"], []).append(row)

    merged_summaries: list[dict[str, Any]] = []
    for variant, rows in by_variant.items():
        ordered_rows = sorted(rows, key=lambda item: item["prompt_id"])
        merged_summaries.append(summarize_results(variant, ordered_rows))
    merged_summaries.sort(key=lambda item: item["variant"])
    return merged_summaries


def filter_benchmark(
    benchmark: list[EvalPrompt],
    prompt_ids: list[str] | None = None,
    category: str | None = None,
) -> list[EvalPrompt]:
    filtered = benchmark
    if prompt_ids:
        wanted = set(prompt_ids)
        filtered = [item for item in filtered if item.prompt_id in wanted]
    if category:
        filtered = [item for item in filtered if item.category == category]
    return filtered


def evaluate_prompt_variant(name: str, system_prompt: str, benchmark: list[EvalPrompt]) -> dict[str, Any]:
    eval_model = get_env("THREADMIND_EVAL_MODEL", get_env("THREADMIND_MODEL", "gpt-4.1-mini"))
    agent = ThreadMindAgent(system_prompt=system_prompt, model=eval_model)
    results: list[dict[str, Any]] = []
    for item in benchmark:
        try:
            run = agent.run(item.user_prompt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] %s failed: %s", name, item.prompt_id, exc)
            result = {
                "variant": name,
                "prompt_id": item.prompt_id,
                "category": item.category,
                "user_prompt": item.user_prompt,
                "expected_mode": item.expected_mode,
                "actual_mode": "error",
                "expected_tools": item.expected_tools,
                "actual_tools": [],
                "mode_correct": False,
                "tool_accuracy": False,
                "action_correct": item.expected_mode != "create_action",
                "over_action": False,
                "clarification_hit": False,
                "out_of_scope_hit": False,
                "latency_seconds": 0.0,
                "answer": f"ERROR: {exc}",
                "reasoning_summary": "Prompt failed during evaluation.",
                "created_action_id": None,
                "citations": {"memory_ids": [], "data_refs": []},
                "tool_calls": [],
                "notes": item.notes,
            }
            results.append(result)
            time.sleep(5.0)
            continue
        used_tools = [call["tool_name"] for call in run.tool_calls]
        acceptable_modes = item.acceptable_modes or [item.expected_mode]
        mode_correct = run.final.mode.value in acceptable_modes
        expected_tools_set = set(item.expected_tools)
        actual_tools_set = set(used_tools)
        tool_accuracy = expected_tools_set == actual_tools_set
        created_action = run.final.created_action_id is not None
        expected_action = item.expected_mode == "create_action"
        action_correct = (created_action and expected_action) or (not created_action and not expected_action)
        over_action = created_action and not expected_action
        clarification_hit = item.category == "ambiguous" and run.final.mode.value == "ask_clarification"
        oos_hit = item.category == "out_of_scope" and run.final.mode.value == "out_of_scope"
        result = {
            "variant": name,
            "prompt_id": item.prompt_id,
            "category": item.category,
            "user_prompt": item.user_prompt,
            "expected_mode": item.expected_mode,
            "actual_mode": run.final.mode.value,
            "expected_tools": item.expected_tools,
            "actual_tools": used_tools,
            "mode_correct": mode_correct,
            "tool_accuracy": tool_accuracy,
            "action_correct": action_correct,
            "over_action": over_action,
            "clarification_hit": clarification_hit,
            "out_of_scope_hit": oos_hit,
            "latency_seconds": round(run.latency_seconds, 3),
            "answer": run.final.answer,
            "reasoning_summary": run.final.reasoning_summary,
            "created_action_id": run.final.created_action_id,
            "citations": run.final.citations,
            "tool_calls": run.tool_calls,
            "notes": item.notes,
        }
        results.append(result)
        logger.info(
            "[%s] %s | mode=%s | tools=%s | latency=%.2fs",
            name,
            item.prompt_id,
            run.final.mode.value,
            used_tools,
            run.latency_seconds,
        )
        time.sleep(5.0)

    return summarize_results(name, results)


def choose_variant(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(
        summaries,
        key=lambda row: (
            -(row["failed_runs"]),
            row["mode_accuracy"],
            row["out_of_scope_precision"],
            1 - row["over_action_rate"],
            row["tool_selection_accuracy"],
        ),
        reverse=True,
    )
    return ranked[0]


def render_markdown_summary(summaries: list[dict[str, Any]], shipped: dict[str, Any]) -> str:
    total_failed = sum(row["failed_runs"] for row in summaries)
    lines = [
        "## Eval Summary",
        "",
        "| Variant | Failed Runs | Mode Acc. | Tool Acc. | Action Precision | Over-action | Clarification on Ambiguous | OOS Precision | Avg Latency (s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        lines.append(
            f"| {row['variant']} | {row['failed_runs']} | {row['mode_accuracy']:.3f} | {row['tool_selection_accuracy']:.3f} | "
            f"{row['action_precision']:.3f} | {row['over_action_rate']:.3f} | "
            f"{row['clarification_rate_on_ambiguous']:.3f} | {row['out_of_scope_precision']:.3f} | {row['average_latency_seconds']:.3f} |"
        )
    lines.append("")
    if total_failed > 0:
        lines.extend(
            [
                "Evaluation status: **invalid run**.",
                "",
                "Reason: one or more prompts failed due to network/API issues, so the ship recommendation and metrics should not be treated as final.",
                "",
                "Per-category mode accuracy:",
            ]
        )
    else:
        lines.extend(
            [
                f"Recommended prompt to ship: **{shipped['variant']}**.",
                "",
                "Reason: it provided the best balance of grounded mode selection, lower over-action behaviour, and stronger handling of ambiguous or out-of-scope prompts.",
                "",
                "Per-category mode accuracy:",
            ]
        )
    for row in summaries:
        lines.append(
            f"- {row['variant']}: happy-path {row['happy_path_mode_accuracy']:.3f}, "
            f"ambiguous {row['ambiguous_mode_accuracy']:.3f}, out-of-scope {row['out_of_scope_mode_accuracy']:.3f}"
        )
    return "\n".join(lines)


def write_csv(rows: list[dict[str, Any]]) -> None:
    path = OUTPUTS_DIR / "eval_results.csv"
    fieldnames = [
        "variant",
        "prompt_id",
        "category",
        "expected_mode",
        "actual_mode",
        "expected_tools",
        "actual_tools",
        "mode_correct",
        "tool_accuracy",
        "action_correct",
        "over_action",
        "clarification_hit",
        "out_of_scope_hit",
        "latency_seconds",
        "created_action_id",
        "answer",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variant",
        choices=["proactive", "conservative", "both"],
        default="both",
        help="Which system prompt variant to evaluate.",
    )
    parser.add_argument(
        "--prompt-id",
        action="append",
        help="Evaluate a specific prompt_id. Can be passed multiple times.",
    )
    parser.add_argument(
        "--category",
        choices=["happy_path", "ambiguous", "out_of_scope"],
        help="Evaluate only one benchmark category.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append these results into the cumulative eval_results files instead of overwriting with only this run.",
    )
    args = parser.parse_args()

    load_env()
    setup_logging()
    ensure_directories()
    benchmark = filter_benchmark(load_benchmark(), prompt_ids=args.prompt_id, category=args.category)
    if not benchmark:
        raise SystemExit("No benchmark prompts matched the requested filters.")

    variants: list[tuple[str, str]] = []
    if args.variant in {"proactive", "both"}:
        variants.append(("proactive", PROACTIVE_PROMPT))
    if args.variant in {"conservative", "both"}:
        variants.append(("conservative", CONSERVATIVE_PROMPT))

    try:
        summaries = [
            evaluate_prompt_variant(variant_name, prompt, benchmark)
            for variant_name, prompt in variants
        ]
    except RuntimeError as exc:
        raise SystemExit(f"{exc}\nSet OPENAI_API_KEY in .env before running `make eval`.") from exc
    if args.append:
        summaries = merge_summaries(load_existing_payload(), summaries)

    shipped = choose_variant(summaries)
    payload = {
        "summaries": summaries,
        "recommended_variant": None if any(row["failed_runs"] > 0 for row in summaries) else shipped["variant"],
        "markdown_summary": render_markdown_summary(summaries, shipped),
    }
    dump_json(OUTPUTS_DIR / "eval_results.json", payload)
    all_rows = [row for summary in summaries for row in summary["results"]]
    write_csv(all_rows)
    print(payload["markdown_summary"])


if __name__ == "__main__":
    main()
