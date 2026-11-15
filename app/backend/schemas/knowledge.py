"""设定中心（画风词缀库 + 设定矛盾审阅）的数据契约（M4）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

AFFIX_CATEGORIES = ("general", "lighting", "camera", "quality", "character")
AffixCategory = Literal["general", "lighting", "camera", "quality", "character"]

ISSUE_CATEGORIES = (
    "character_conflict",
    "asset_missing_ref",
    "dialogue_speaker",
    "prop_mismatch",
    "continuity",
)
IssueCategory = Literal[
    "character_conflict",
    "asset_missing_ref",
    "dialogue_speaker",
    "prop_mismatch",
    "continuity",
]

ISSUE_SEVERITIES = ("info", "warning", "error")
IssueSeverity = Literal["info", "warning", "error"]

ISSUE_STATUSES = ("open", "adopted", "ignored")
IssueStatus = Literal["open", "adopted", "ignored"]


class StyleAffixIn(BaseModel):
    """新增画风词缀模板。"""

    name: str = Field(..., min_length=1, max_length=60)
    category: AffixCategory = "general"
    positive: str = Field("", max_length=1500)
    negative: str = Field("", max_length=1500)
    is_default: bool = False
    sort_order: int = Field(0, ge=0, le=9999)

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("词缀名称不能为空")
        return text

    @field_validator("positive", "negative", mode="before")
    @classmethod
    def _clean_prompt(cls, value: object) -> str:
        return str(value or "").strip()

    @model_validator(mode="after")
    def _ensure_non_empty_prompt(self) -> "StyleAffixIn":
        if not self.positive and not self.negative:
            raise ValueError("正向词缀与负向词缀至少填写一项")
        return self


class StyleAffixUpdateIn(StyleAffixIn):
    """编辑画风词缀模板（全量更新）。"""


class StyleAffixOut(BaseModel):
    id: int
    project_id: int
    name: str
    category: str
    positive: str
    negative: str
    is_default: bool
    sort_order: int
    created_by: int | None = None
    created_at: str
    updated_at: str


class ConsistencyIssueOut(BaseModel):
    id: int
    project_id: int
    issue_key: str
    category: str
    severity: str
    title: str
    description: str
    source_type: str
    source_id: int | None = None
    suggestion: str
    status: str
    resolved_by: int | None = None
    resolved_by_name: str = ""
    resolved_at: str | None = None
    created_at: str
    updated_at: str


class ConsistencyScanOut(BaseModel):
    project_id: int
    scanned_shots: int
    scanned_assets: int
    scanned_docs: int
    new_count: int
    reopened_count: int
    open_count: int
    issues: list[ConsistencyIssueOut]


class ConsistencyResolveIn(BaseModel):
    action: Literal["adopt", "ignore", "reopen"]
