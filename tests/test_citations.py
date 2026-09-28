import pytest
from pydantic import ValidationError

from signaldesk.citations.models import Citation, CitationKind
from signaldesk.citations.registry import CitationRegistry


def test_direct_requires_source():
    with pytest.raises(ValidationError):
        Citation(id="C1", kind=CitationKind.DIRECT, value=1.0, label="x")


def test_derived_requires_formula_and_parents():
    with pytest.raises(ValidationError):
        Citation(id="C2", kind=CitationKind.DERIVED, value=1.0, label="x", formula="f()")
    with pytest.raises(ValidationError):
        Citation(id="C3", kind=CitationKind.DERIVED, value=1.0, label="x", derived_from=["C1"])


def test_registry_rejects_dangling_derived_from():
    reg = CitationRegistry()
    direct = reg.register_direct(100.0, "BTC close", source_tool="quotes", column="price")
    derived = reg.register_derived(55.0, "BTC RSI", formula="RSI(close,14)", derived_from=[direct.id])
    assert derived.id in reg.all()
    with pytest.raises(KeyError):
        reg.register_derived(1.0, "bad", formula="f", derived_from=["C999"])


def test_ids_are_sequential():
    reg = CitationRegistry()
    a = reg.register_direct(1, "a", source_tool="t", column="c")
    b = reg.register_direct(2, "b", source_tool="t", column="c")
    assert (a.id, b.id) == ("C1", "C2")