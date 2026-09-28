"""Per-run citation registry: hands out ids and stores every citation."""
from __future__ import annotations

from .models import Citation, CitationKind


class CitationRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Citation] = {}
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"C{self._counter}"

    def register_direct(
        self,
        value: float | int | str,
        label: str,
        *,
        source_tool: str,
        column: str,
        file: str | None = None,
        row_key: str | None = None,
    ) -> Citation:
        cite = Citation(
            id=self._next_id(),
            kind=CitationKind.DIRECT,
            value=value,
            label=label,
            source_tool=source_tool,
            column=column,
            file=file,
            row_key=row_key,
        )
        self._items[cite.id] = cite
        return cite

    def register_derived(
        self,
        value: float | int | str,
        label: str,
        *,
        formula: str,
        derived_from: list[str],
    ) -> Citation:
        missing = [cid for cid in derived_from if cid not in self._items]
        if missing:
            raise KeyError(f"derived_from references unknown citations: {missing}")
        cite = Citation(
            id=self._next_id(),
            kind=CitationKind.DERIVED,
            value=value,
            label=label,
            formula=formula,
            derived_from=list(derived_from),
        )
        self._items[cite.id] = cite
        return cite

    def get(self, citation_id: str) -> Citation:
        return self._items[citation_id]

    def all(self) -> dict[str, Citation]:
        return dict(self._items)

    def __len__(self) -> int:
        return len(self._items)
