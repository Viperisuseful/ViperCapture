"""Validated best-effort bulk async render submission contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from render_contract import RenderRequest


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BulkJobItem(StrictModel):
    id: str | None = Field(default=None, min_length=1, max_length=128)
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    render: RenderRequest


class BulkJobRequest(StrictModel):
    items: list[BulkJobItem] = Field(min_length=1, max_length=100)
