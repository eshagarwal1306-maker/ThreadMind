"""Pydantic schemas for ThreadMind."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class MemoryType(str, Enum):
    decision = "decision"
    open_issue = "open_issue"
    deadline = "deadline"
    owner_update = "owner_update"
    risk = "risk"
    customer_context = "customer_context"
    supplier_context = "supplier_context"


class MemoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    type: MemoryType
    topic: str
    summary: str
    owner: Optional[str] = None
    source_channel: str
    source_thread: str
    created_at: str
    confidence: float = Field(ge=0.0, le=1.0)


class SearchMemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    top_k: int = Field(default=5, ge=1, le=10)


class QueryStructuredDataArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str


class ActionType(str, Enum):
    follow_up = "follow_up"
    escalation = "escalation"
    discrepancy_alert = "discrepancy_alert"
    decision_record = "decision_record"


class Priority(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class CreateActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: ActionType
    title: str = Field(min_length=5, max_length=140)
    priority: Priority
    reason: str = Field(min_length=10, max_length=500)
    evidence: Dict[str, Any]
    owner: Optional[str] = None
    related_entity: Optional[str] = None


class FinalMode(str, Enum):
    answer = "answer"
    ask_clarification = "ask_clarification"
    out_of_scope = "out_of_scope"
    create_action = "create_action"


class FinalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: FinalMode
    answer: str
    reasoning_summary: str = Field(min_length=5, max_length=400)
    used_tools: List[str]
    citations: Dict[str, List[str]]
    next_steps: List[str] = []
    created_action_id: Optional[str] = None


class ActionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    created_at: str
    action_type: ActionType
    title: str
    priority: Priority
    owner: Optional[str] = None
    related_entity: Optional[str] = None
    reason: str
    evidence: Dict[str, Any]
    status: str = "open"


class EvalPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_id: str
    category: str
    user_prompt: str
    expected_mode: str
    expected_tools: List[str]
    notes: str
    expected_action_type: Optional[str] = None
    acceptable_modes: Optional[List[str]] = None


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
