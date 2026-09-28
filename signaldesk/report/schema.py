"""Report contracts: ScanReport (WF-1/3/4) and DeepDiveReport (WF-2)."""
from __future__ import annotations

from pydantic import BaseModel, computed_field

from signaldesk.citations.models import Citation

DISCLAIMER = (
    "Not financial advice. SignalDesk performs automated technical research; "
    "signals are heuristic scores, not predictions or guarantees of return. "
    "Markets involve risk of loss. Always do your own research."
)


class SentimentReading(BaseModel):
    index: str
    value: float
    regime: str
    citations: list[str] = []


class EntryPlan(BaseModel):
    """Refined intraday entry plan (workflows.md Phase 7.5)."""
    mode: str  # "market" | "pullback" | "wait"
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk_daily: float
    risk_refined: float
    note: str = ""
    roles: dict[str, str] = {}                      # role -> timeframe (bias/structure/...)
    timeframes: dict[str, dict[str, float]] = {}    # tf -> cited feature snapshot
    citations: list[str] = []
    cost_pct: float = 0.0            # assumed round-trip cost, % of notional
    cost_in_r: float = 0.0           # that cost expressed in R units
    risk_floored: bool = False       # the risk floor widened the refined stop


class Signal(BaseModel):
    symbol: str
    direction: str = "LONG"
    score: float
    entry: float
    stop: float
    tp1: float
    tp2: float
    rr: str = "2.0R / 3.0R"
    confluence: list[str] = []
    catalysts: list[str] = []
    citations: list[str] = []
    entry_plan: EntryPlan | None = None  # Phase 7.5 intraday refinement
    cost_pct: float = 0.0                # assumed round-trip cost, % of notional
    cost_in_r: float = 0.0               # daily-plan cost-to-risk ratio


class AvoidEntry(BaseModel):
    symbol: str
    reason: str
    citations: list[str] = []


class NewsClaim(BaseModel):
    claim: str
    url: str = ""
    published: str = ""


class ScanReport(BaseModel):
    market: str
    as_of: str
    scoring_preset: str
    universe: list[str]
    context_summary: str
    context_claims: list[NewsClaim] = []
    sentiment: list[SentimentReading] = []
    signals: list[Signal] = []
    avoid: list[AvoidEntry] = []
    disclosures: list[str] = []
    citations: dict[str, Citation] = {}
    charts: dict[str, dict[str, str]] = {}  # symbol -> {timeframe: chart PNG path} (run-relative)
    disclaimer: str = DISCLAIMER

    @computed_field  # serialized into report.json for the UI and the API
    @property
    def citation_coverage(self) -> float:
        """Share of signals whose every core number is cited (R1/R5)."""
        if not self.signals:
            return 1.0
        cited = sum(1 for s in self.signals if s.citations and all(cid in self.citations for cid in s.citations))
        return cited / len(self.signals)


# --- WF-2 Ticker Deep Dive -----------------------------------------------------


class FundamentalsSnapshot(BaseModel):
    name: str = ""
    sector: str = ""
    industry: str = ""
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    price_to_book: float | None = None
    profit_margins: float | None = None
    roe: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    recommendation: str = ""
    target_price: float | None = None
    citations: list[str] = []


class EarningsRow(BaseModel):
    date: str
    eps_estimate: float | None = None
    eps_actual: float | None = None
    surprise_pct: float | None = None


class EdgarAnnual(BaseModel):
    fy: str
    revenue: float | None = None
    net_income: float | None = None


class DeepDiveReport(BaseModel):
    symbol: str
    as_of: str
    name: str = ""
    summary: str = ""
    technicals: dict = {}          # features + score breakdown
    chart: str = ""                # run-relative path to the TA chart PNG
    charts: dict[str, dict[str, str]] = {}  # symbol -> {timeframe: chart PNG path}
    signal: Signal | None = None   # present when score clears the threshold
    fundamentals: FundamentalsSnapshot = FundamentalsSnapshot()
    earnings_history: list[EarningsRow] = []
    earnings_next: str = ""
    annuals: list[EdgarAnnual] = []    # SEC EDGAR (may be empty if unavailable)
    bull_case: list[NewsClaim] = []
    bear_case: list[NewsClaim] = []
    catalysts: list[NewsClaim] = []
    disclosures: list[str] = []
    citations: dict[str, Citation] = {}
    disclaimer: str = DISCLAIMER

    @computed_field
    @property
    def citation_coverage(self) -> float:
        """Deep dives cite the technicals and fundamentals they assert."""
        return 1.0 if self.citations else 0.0
