from __future__ import annotations

from pydantic import BaseModel, Field


class TerminalScreenResult(BaseModel):
    terminal_id: str
    columns: int = Field(ge=20)
    rows: int = Field(ge=5)
    display: str
    cursor: int = Field(ge=0)
    alive: bool
    busy: bool
