"""Validated best-effort bulk async render submission contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from render_contract import RenderRequest

# A bulk request may carry 100 independently valid renders; without an
# aggregate cap, 100 near-5 MiB sources approach 500 MiB of accepted source
# text in one JSON body. Bounding the combined embedded source keeps a single
# valid bulk request from exhausting process memory.
MAX_BULK_SOURCE_BYTES = 20 * 1024 * 1024

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

    @model_validator(mode="after")
    def validate_aggregate_source(self) -> "BulkJobRequest":
        total = 0
        for item in self.items:
            for source in (item.render.html, item.render.markdown):
                if source is not None:
                    total += len(source.encode("utf-8"))
        if total > MAX_BULK_SOURCE_BYTES:
            raise ValueError(
                "combined embedded html/markdown source across items must not "
                "exceed 20 MiB"
            )
        return self
