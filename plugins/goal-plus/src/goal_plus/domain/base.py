"""Pure contracts and structural invariants."""

from __future__ import annotations

from pydantic import (
    BaseModel,
    ConfigDict,
)


class SearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)
