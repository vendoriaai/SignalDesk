import React, { useState } from "react";

const ICONS = { phase: "🧭", step: "•", tool: "🔧", search: "🔎", sandbox: "🧮", chart: "📈", analysis: "🧠", result: "✅", warn: "⚠️" };
const TREND_ICON = { up: "▲", down: "▼", range: "◆" };

function AiRead({ data }) {
  const reads = Object.entries(data.reads || {});
  const confidence =
    data.confidence != null ? Math.round((Number(data.confidence) || 0) * 100) : null;
  if (data.direction && data.score != null && !reads.length) {
    // Phase 6.5 generation decision: direction + conviction + rationale
    const dir = String(data.direction);
    return (
      <div className="ai-read">
        <div className="ai-gen-line">
          <span className={`ai-dir ai-dir-${dir.toLowerCase()}`}>{dir}</span>
          {data.score != null && <span className="ai-score">score {data.score}/100</span>}
          {data.invalidation != null && (
            <span className="ai-inval">invalidation {Number(data.invalidation).toPrecision(6)}</span>
          )}
        </div>
        {data.rationale ? <p className="ai-rationale">“{data.rationale}”</p> : null}
        {data.model && <div className="ai-meta"><span className="ai-model">{data.model}</span></div>}
      </div>
    );
  }
  return (
    <div className="ai-read">
      {reads.length > 0 && (
        <div className="ai-read-tfs">
          {reads.map(([tf, r]) => {
            const trend = (r.trend || "").toLowerCase();
            return (
              <div key={tf} className={`ai-tf ai-tf-${trend || "range"}`}>
                <span className="ai-tf-name">{tf}</span>
                <span className="ai-tf-trend">
                  {TREND_ICON[trend] || "◆"} {r.trend || "n/a"}
                </span>
                {r.note ? <span className="ai-tf-note">{r.note}</span> : null}
              </div>
            );
          })}
        </div>
      )}
      {data.rationale ? <p className="ai-rationale">“{data.rationale}”</p> : null}
      {(data.entry != null || data.model || confidence != null) && (
        <div className="ai-meta">
          {data.entry != null && (
            <span className="ai-levels">
              entry <b>{Number(data.entry).toPrecision(6)}</b>
              {data.stop != null && <> · stop <b>{Number(data.stop).toPrecision(6)}</b></>}
              {data.tp1 != null && <> · TP1 <b>{Number(data.tp1).toPrecision(6)}</b></>}
              {data.tp2 != null && <> · TP2 <b>{Number(data.tp2).toPrecision(6)}</b></>}
            </span>
          )}
          {confidence != null && <span className="ai-conf">confidence {confidence}%</span>}
          {data.model && <span className="ai-model">{data.model}</span>}
        </div>
      )}
    </div>
  );
}

export default function StepCard({ event, runId }) {
  const [open, setOpen] = useState(Boolean(event.data));
  const isChart = event.kind === "chart";
  const isAnalysis = event.kind === "analysis";
  const chartName = isChart && event.data?.path ? event.data.path.split(/[\\/]/).pop() : "";
  return (
    <div className={`step step-${event.kind}`} onClick={() => setOpen(!open)}>
      <span className="step-icon">{ICONS[event.kind] || "•"}</span>
      <span className="step-phase">{event.phase}</span>
      <span className="step-msg">{event.message}</span>
      {isAnalysis && event.data && Object.keys(event.data).length > 0 && (
        <AiRead data={event.data} />
      )}
      {open && event.data && Object.keys(event.data).length > 0 && !isAnalysis && (
        <pre className="step-data">{JSON.stringify(event.data, null, 2)}</pre>
      )}
      {isChart && chartName && runId && (
        <img
          className="step-chart-img"
          src={`/api/runs/${runId}/charts/${chartName}`}
          alt={event.message}
          loading="lazy"
          onClick={(e) => e.stopPropagation()}
        />
      )}
    </div>
  );
}
