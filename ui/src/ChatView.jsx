import React, { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { postChat, getRun, wsUrl } from "./api.js";
import StepCard from "./StepCard.jsx";

function classifyNextView(report) {
  return report ? "report" : "trace";
}

export default function ChatView() {
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [events, setEvents] = useState([]);
  const [reportMd, setReportMd] = useState("");
  const [reportJson, setReportJson] = useState(null);
  const [charts, setCharts] = useState({});
  const [runId, setRunId] = useState("");
  const [runMeta, setRunMeta] = useState(null);
  const [demo, setDemo] = useState(false);
  const [err, setErr] = useState("");
  const sockRef = useRef(null);

  useEffect(() => () => sockRef.current?.close(), []);

  async function submit(e) {
    e.preventDefault();
    if (!prompt.trim() || busy) return;
    setBusy(true);
    setErr("");
    setEvents([]);
    setReportMd("");
    setReportJson(null);
    setCharts({});
    setRunId("");
    const started = await postChat(prompt, demo);
    if (started.error) {
      setErr(started.error);
      setBusy(false);
      return;
    }
    setRunMeta(started);
    setRunId(started.run_id);
    const ws = new WebSocket(wsUrl(started.run_id));
    sockRef.current = ws;
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.kind === "done") {
        ws.close();
        getRun(started.run_id).then((d) => {
          setReportMd(d.report_md || "");
          setReportJson(d.report_json || null);
          setCharts(d.report_json?.charts || (d.report_json?.chart ? { [d.report_json.symbol]: d.report_json.chart } : {}));
        }).finally(() => setBusy(false));
      } else if (msg.kind !== "ping") {
        setEvents((prev) => [...prev, msg]);
      }
    };
    ws.onerror = () => {
      // fall back to polling run detail
      const poll = setInterval(async () => {
        const d = await getRun(started.run_id);
        setEvents(d.events || []);
        if (d.status !== "running") {
          clearInterval(poll);
          setReportMd(d.report_md || "");
          setReportJson(d.report_json || null);
          setCharts(d.report_json?.charts || {});
          setBusy(false);
        }
      }, 1000);
    };
  }

  return (
    <div className="chat">
      <form className="prompt-bar" onSubmit={submit}>
        <input
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder='e.g. "scan the crypto market" · "deep dive AAPL before earnings"'
        />
        <label className="demo-toggle">
          <input type="checkbox" checked={demo} onChange={(e) => setDemo(e.target.checked)} />
          demo (offline)
        </label>
        <button disabled={busy}>{busy ? "…" : "Run"}</button>
      </form>
      {err && <div className="error">{err}</div>}
      {runMeta?.plan && (
        <div className="plan-line">
          workflow={runMeta.plan.workflow} · market={runMeta.plan.market}
          {runMeta.plan.symbol ? ` · symbol=${runMeta.plan.symbol}` : ""}
          {runMeta.plan.watchlist ? ` · watchlist=${runMeta.plan.watchlist}` : ""}
        </div>
      )}
      {classifyNextView(reportMd) === "trace" && events.length > 0 && (
        <div className="trace">
          <h3>Execution trace</h3>
          {events.map((ev, i) => (
            <StepCard key={i} event={ev} />
          ))}
        </div>
      )}
      {reportMd && Object.keys(charts).length > 0 && (
        <div className="charts-grid">
          <h3>Charts analyzed</h3>
          <div className="charts-row">
            {Object.entries(charts).map(([sym, tfs]) =>
              (typeof tfs === "object"
                ? Object.entries(tfs)
                : [["1d", tfs]]
              ).map(([tf, rel]) => (
                <figure key={`${sym}-${tf}`} className="chart-card">
                  <img
                    src={`/api/runs/${runId}/charts/${sym}-${tf}.png`}
                    alt={`${sym} ${tf} TA chart`}
                    loading="lazy"
                  />
                  <figcaption>{sym} · {tf}</figcaption>
                </figure>
              ))
            )}
          </div>
        </div>
      )}
      {reportMd && (
        <div className="report">
          <Markdown remarkPlugins={[remarkGfm]}>{reportMd}</Markdown>
        </div>
      )}
    </div>
  );
}
