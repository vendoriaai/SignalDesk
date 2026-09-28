"""News depth: article extraction (read the news, not just the snippet) and
the CoinGecko-native Altcoin Season Index."""
import httpx

from signaldesk.tools import extract, sentiment


class _FakeResp:
    def __init__(self, *, json_data=None, text="", headers=None, status=200):
        self._json = json_data or {}
        self.text = text
        self.headers = headers or {}
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise httpx.HTTPStatusError("http error", request=None,
                                        response=None)  # type: ignore[arg-type]

    def json(self):
        return self._json


# HTML stripping ----------------------------------------------------------------

def test_strip_html_removes_script_and_keeps_text():
    markup = """<html><head><style>body{color:red}</style></head>
    <body><nav>menu junk</nav><article><h1>MarsCoin rallies</h1>
    <p>Institutional flows accelerated after the ETF approval.</p>
    <script>trackMe()</script><p>Traders expect follow-through.</p></article></body></html>"""
    text = extract._strip_html(markup)
    assert "MarsCoin rallies" in text
    assert "Institutional flows accelerated" in text
    assert "trackMe" not in text and "menu junk" not in text


def test_first_sentences_cuts_at_sentence_boundary():
    text = "First sentence is here. Second sentence follows. " + "x" * 700
    out = extract._first_sentences(text, 120)
    assert out.endswith("follows.") and len(out) <= 120
    assert not out.endswith("x" * 10)


# extract_article ----------------------------------------------------------------

def test_direct_fetch_extracts_article_text(monkeypatch):
    html = "<html><body><p>Bitcoin funds saw record inflows.</p><p>Analysts turned bullish.</p></body></html>"
    monkeypatch.setattr(extract.httpx, "get",
                        lambda url, **kw: _FakeResp(text=html, headers={"content-type": "text/html"}))
    out = extract.extract_article("https://example.com/news", max_chars=200)
    assert out.startswith("Bitcoin funds saw record inflows.")


def test_direct_fetch_rejects_non_html(monkeypatch):
    monkeypatch.setattr(extract.httpx, "get",
                        lambda url, **kw: _FakeResp(text="PK", headers={"content-type": "application/pdf"}))
    assert extract.extract_article("https://example.com/file.pdf") == ""


def test_tavily_extract_preferred_when_key_present(monkeypatch):
    def fake_post(url, **kw):
        assert "tavily" in url
        return _FakeResp(json_data={"results": [{"raw_content": "Tavily body text."}]})

    monkeypatch.setattr(extract.httpx, "post", fake_post)

    def boom(url, **kw):  # direct fetch must not be reached
        raise AssertionError("direct fetch should not run when Tavily succeeds")

    monkeypatch.setattr(extract.httpx, "get", boom)
    assert extract.extract_article("https://example.com/a", tavily_api_key="k") == "Tavily body text."


def test_extract_article_degrades_to_empty(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(extract.httpx, "get", boom)
    assert extract.extract_article("https://example.com/a") == ""
    assert extract.extract_article("ftp://nope") == ""
    assert extract.extract_article("") == ""


# Altcoin Season Index (CoinGecko-native) -----------------------------------------

def _coingecko_rows():
    return [
        {"id": "bitcoin", "price_change_percentage_30d_in_currency": 5.0},
        {"id": "ethereum", "price_change_percentage_30d_in_currency": 9.0},
        {"id": "solana", "price_change_percentage_30d_in_currency": 2.0},
        {"id": "cardano", "price_change_percentage_30d_in_currency": None},
        {"id": "ripple", "price_change_percentage_30d_in_currency": 6.0},
    ]


def test_altseason_computed_from_coingecko(tmp_path, monkeypatch):
    monkeypatch.setattr(sentiment.httpx, "get",
                        lambda url, **kw: _FakeResp(json_data=_coingecko_rows()))
    res = sentiment.AltSeasonTool(tmp_path).run()
    assert not res.degraded
    assert res.summary.startswith("Altcoin Season Index: 66.7 (mixed)")  # 2 of 3 alts beat BTC
    assert res.csv_files and res.sources[0].name == "coingecko"


def test_altseason_degrades_without_btc_or_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sentiment.httpx, "get",
                        lambda url, **kw: _FakeResp(json_data=[{"id": "ethereum",
                                                                "price_change_percentage_30d_in_currency": 9.0}]))
    assert sentiment.AltSeasonTool(tmp_path).run().degraded        # no BTC baseline

    def boom(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sentiment.httpx, "get", boom)
    assert sentiment.AltSeasonTool(tmp_path).run().degraded


def test_altseason_extremes(tmp_path, monkeypatch):
    rows = [{"id": "bitcoin", "price_change_percentage_30d_in_currency": -1.0}] + [
        {"id": f"c{i}", "price_change_percentage_30d_in_currency": 3.0} for i in range(10)]
    monkeypatch.setattr(sentiment.httpx, "get", lambda url, **kw: _FakeResp(json_data=rows))
    res = sentiment.AltSeasonTool(tmp_path).run()
    assert "100.0 (altcoin season)" in res.summary


# Scan wiring: catalysts quote the article, snippets stay the fallback ---------

def test_scan_catalysts_quote_extracted_articles(tmp_path, monkeypatch):
    import pandas as pd

    from signaldesk.agent.events import EventBus
    from signaldesk.tools import demo
    from signaldesk.tools.base import Source, Tool, ToolResult
    from signaldesk.tools.search import SearchHit
    from signaldesk.workflows import market_scan as ms
    from signaldesk.workflows.market_scan import MarketScanRequest, ToolSet, run_market_scan

    class StubMovers(Tool):
        name = "market_movers"

        def __init__(self, d):
            self.d = d
            d.mkdir(parents=True, exist_ok=True)

        def run(self, per_side=10):
            path = self.d / "movers.csv"
            pd.DataFrame([{"symbol": "SUIUSD", "price": 3.6, "change_24h_pct": 9.0,
                           "volume": 1.9e8, "market_cap": 1.0e10, "side": "gainer"}]
                         ).to_csv(path, index=False)
            return ToolResult(csv_files=[path], summary="stub",
                              sources=[Source(name="stub")])

    class StubOHLCV(demo.DemoOHLCVTool):
        pass  # demo data under a non-Demo class name so the live path runs

    class StubSearch:
        name = "web_search"

        def available(self):
            return True

        def batch(self, queries, max_results=3):
            return {q: [SearchHit(title=f"t: {q}", url="https://example.com/news/marscoin",
                                  snippet="raw search snippet", published="2026-09-28")]
                    for q in queries}

    monkeypatch.setattr(
        ms.extract_mod, "extract_article",
        lambda url, **kw: "Institutional flows accelerated after the ETF approval on Tuesday.")

    d = tmp_path / "artifacts"
    tools = ToolSet(
        movers=StubMovers(d), quotes=demo.DemoQuotesTool(d, "crypto"),
        ohlcv=StubOHLCV(d, "crypto"), search=StubSearch(),
        fear_greed=demo.DemoFearGreedTool(d), altseason=demo.DemoAltSeasonTool(d),
    )
    run = run_market_scan(MarketScanRequest(market="crypto", universe_size=6),
                          tools, EventBus(), tmp_path)
    r = run.report
    assert any("Institutional flows accelerated" in c
               for s in r.signals for c in s.catalysts)
    assert any(c.column == "extracted_text" for c in r.citations.values())
    assert any(c.claim.startswith("Institutional flows") for c in r.context_claims)
    assert any("News depth" in d for d in r.disclosures)
