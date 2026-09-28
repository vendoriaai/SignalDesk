"""Provenance records for every number that appears in a report (TAD 3.4).

Two kinds (workflows.md R1):
- direct: a raw figure pulled from a tool artifact (file + row + column)
- derived: a computed figure, carrying the formula and the citations it was
  computed from
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, model_validator


class CitationKind(str, Enum):
    DIRECT = "direct"
    DERIVED = "derived"


class Citation(BaseModel):
    id: str
    kind: CitationKind
    value: float | int | str
    label: str
    # direct
    source_tool: str | None = None
    file: str | None = None
    row_key: str | None = None
    column: str | None = None
    # derived
    formula: str | None = None
    derived_from: list[str] = []

    @model_validator(mode="after")
    def _check_shape(self) -> "Citation":
        if self.kind is CitationKind.DIRECT and not (self.source_tool and self.column):
            raise ValueError("direct citation requires source_tool and column")
        if self.kind is CitationKind.DERIVED and not (self.formula and self.derived_from):
            raise ValueError("derived citation requires formula and derived_from")
        return self

    def footnote(self) -> str:
        if self.kind is CitationKind.DIRECT:
            where = f"{self.source_tool}:{self.column}"
            if self.file:
                where += f" in {self.file}"
            if self.row_key:
                where += f" [{self.row_key}]"
            return f"[{self.id}] {self.label} = {self.value} ({where})"
        parents = ", ".join(self.derived_from)
        return f"[{self.id}] {self.label} = {self.value} ({self.formula} <- {parents})"
