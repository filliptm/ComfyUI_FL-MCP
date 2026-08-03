"""Shared data models for Ren's lightweight web research pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WebLink(BaseModel):
    """A normalized link discovered on a fetched page."""

    model_config = ConfigDict(frozen=True)

    url: str
    text: str = ""
    title: str | None = None


class WebImageCandidate(BaseModel):
    """Image metadata discovered without downloading the image body."""

    model_config = ConfigDict(frozen=True)

    url: str
    source_url: str
    alt: str = ""
    title: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    """A bounded HTTP response ready for local extraction."""

    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    content: bytes
    text: str
    fetched_at: datetime
    elapsed_ms: int
    redirect_chain: tuple[str, ...] = ()


class ExtractedWebPage(BaseModel):
    """Normalized page content used by search, citations, and image discovery."""

    requested_url: str
    final_url: str
    canonical_url: str
    title: str | None = None
    description: str | None = None
    language: str | None = None
    text: str
    markdown: str
    links: list[WebLink] = Field(default_factory=list)
    images: list[WebImageCandidate] = Field(default_factory=list)
    content_hash: str
    content_type: str
    status_code: int
    fetched_at: datetime
    elapsed_ms: int
    extraction_method: str = "selectolax-local"
    from_cache: bool = False
    quality_score: float = Field(ge=0, le=1)
    requires_hosted_fallback: bool = False
    warnings: list[str] = Field(default_factory=list)
