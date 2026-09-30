import React, { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { listRuns, getRun } from "./api.js";
import StepCard from "./StepCard.jsx";

export default function HistoryView() {
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState(null);
  const [detail, setDetail] = useState(null);
  const pollRef = useRef(null);

  useEffect(() => {
    listRuns().then(setRuns).catch(() => {});
    return () => clearInterval(pollRef.current);
  }, []);

  function poll(id) {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const d = await getRun(id);
        if (d.error) {
          clearInterval(pollRef.current);
          return;
        }
        setDetail(d);
        if (d.status !== "running" && d.live_status !== "running") {
          clearInterval(pollRef.current);
          listRuns().then(setRuns).catch(() => {});
        }
      } catch {
        clearInterval(pollRef.current);
      }
    }, 1500);
  }

  async function open(id) {
    setSelected(id);
    try {
      const d = await getRun(id);
      setDetail(d);
      if (d.status === "running" || d.live_status === "running") poll(id);
    } catch {
      setDetail(null);
    }
  }

  const reportJson = detail?.report_json || null;
  const chartsObj = reportJson?.charts
    ? reportJson.charts
    : reportJson?.chart
      ? { [reportJson.symbol]: reportJson.chart }
      : {};
  const isRunning = Boolean(
    detail && (detail.status === "running" || detail.live_status === "running")
  );

  return (
    <div className="history">
      <div className="history-list">
        <h3>Runs</h3>
        {runs.length === 0 && <p className="muted">No runs yet — history persists offline.</p>}
        {runs.map((r) => (
          <div key={r.id} className={`history-item ${selected === r.id ? "active" : ""}`} onClick={() => open(r.id)}>
            <div className="hi-title">{r.workflow} · {r.market}</div>
            <div className="hi-sub">
              <span className={`status status-${r.status}`}>{r.status}</span> · {r.started_at.slice(0, 16).replace("T", " ")}
            </div>
          </div>
        ))}
      </div>
      <div className="history-detail">
        {detail ? (
          <>
            {!detail.report_md && (
              <p className="muted">
                {isRunning
                  ? "Run in progress — the execution trace below streams live."
                  : detail.error || "No report (run failed or was pruned)"}
              </p>
            )}
            {detail.events?.length > 0 && (
              <details className="trace" open>
                <summary>
                  Execution trace ({detail.events.length} steps){isRunning ? " · live" : ""}
                </summary>
                {detail.events.map((ev, i) => (
                  <StepCard key={i} event={ev} runId={detail.id} />
                ))}
              </details>
            )}
            {detail.report_md && Object.keys(chartsObj).length > 0 && (
              <div className="charts-grid">
                <h3>Charts analyzed</h3>
                <div className="charts-row">
                  {Object.entries(chartsObj).map(([sym, tfs]) =>
                    (typeof tfs === "object"
                      ? Object.entries(tfs)
                      : [["1d", tfs]]
                    ).map(([tf, rel]) => (
                      <figure key={`${sym}-${tf}`} className="chart-card">
                        <img
                          src={`/api/runs/${detail.id}/charts/${sym}-${tf}.png`}
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
            {detail.report_md && (
              <div className="report"><Markdown remarkPlugins={[remarkGfm]}>{detail.report_md}</Markdown></div>
            )}
          </>
        ) : (
          <p className="muted">Select a run to view its report.</p>
        )}
      </div>
    </div>
  );
}
