"""Feedback request and response models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FeedbackRequest(BaseModel):
    """Request model for feedback endpoint."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    relevant_paths: list[str] | None = None
    irrelevant_paths: list[str] | None = None


class FeedbackResponse(BaseModel):
    """Response model for feedback endpoint."""

    model_config = ConfigDict(extra="forbid")

    applied: bool
    adjustments_count: int
