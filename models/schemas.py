"""Pydantic v2 models for Aim + Digest product nouns."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AimCreate(BaseModel):
    user_id: str
    title: str
    summary: list[str]
    monitored_entities: list[str]
    regions: list[str]
    update_types: list[str]


class Aim(AimCreate):
    aim_id: str
    created_at: str
    updated_at: str


class AimUpdate(BaseModel):
    user_id: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[list[str]] = None
    monitored_entities: Optional[list[str]] = None
    regions: Optional[list[str]] = None
    update_types: Optional[list[str]] = None


class DigestItem(BaseModel):
    title: str
    body: str
    source_urls: list[str]
    source_count: int
    item_type: str
    relevance_score: int


class DigestSection(BaseModel):
    title: str
    items: list[DigestItem]


class Digest(BaseModel):
    digest_id: str
    aim_id: str
    headline: str
    date_range: str
    sections: list[DigestSection]
    generated_at: str
    status: str = "queued"
    mode: str
    funnel: dict[str, Any] = Field(default_factory=dict)
