import React, { useState } from "react";

const ICONS = { phase: "🧭", step: "•", tool: "🔧", search: "🔎", sandbox: "🧮", chart: "📈", result: "✅", warn: "⚠️" };

export default function StepCard({ event }) {
  const [open, setOpen] = useState(Boolean(event.data));
  return (
    <div className={`step step-${event.kind}`} onClick={() => setOpen(!open)}>
      <span className="step-icon">{ICONS[event.kind] || "•"}</span>
      <span className="step-phase">{event.phase}</span>
      <span className="step-msg">{event.message}</span>
      {open && event.data && Object.keys(event.data).length > 0 && (
        <pre className="step-data">{JSON.stringify(event.data, null, 2)}</pre>
      )}
    </div>
  );
}
