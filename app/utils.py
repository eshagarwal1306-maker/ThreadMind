"""Utility helpers."""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
OUTPUTS_DIR = ROOT_DIR / "outputs"
DB_PATH = ROOT_DIR / "threadmind.db"


def ensure_directories() -> None:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)


def load_env() -> None:
    load_dotenv(ROOT_DIR / ".env")


def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.getenv(name, default)


def dump_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)
