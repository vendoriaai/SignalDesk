import React, { useEffect, useState } from "react";
import { getOutcomes, paperFill, paperMiss, resolveOutcomes } from "./api.js";

const fmtR = (x) => (x == null || x === undefined ? "—" : `${x >= 0 ? "+" : ""}${Number(x).toFixed(2)}R`);
const fmtPct = (x) => (x == null || x === undefined ? "—" : `${(x * 100).toFixed(1)}%`);
const fmtPx = (x) => (x == null || x === undefined ? "—" : Number(x).toPrecision(6).replace(/\.?0+$/, ""));
// profit factor is +inf with no losing trades; JSON has no Infinity (arrives as null)
const fmtPf = (x) => (x == null || x === undefined || !Number.isFinite(Number(x)) ? "∞" : Number(x).toFixed(2));

function StatCard({ label, value, sub }) {
  return (
    <div className="stat-card">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

function Breakdown({ title, groups }) {
  const entries = Object.entries(groups || {}).filter(([, v]) => v.n);
  if (entries.length < 2) return null;
  return (
    <div className="oc-breakdown">
      <h3>By {title}</h3>
      <table className="oc-table">
        <thead><tr><th>group</th><th>n</th><th>expectancy</th><th>hit rate</th></tr></thead>
        <tbody>
          {entries.map(([name, v]) => (
            <tr key={name}>
              <td>{name}</td><td>{v.n}</td>
              <td>{fmtR(v.expectancy_r)}</td><td>{fmtPct(v.hit_rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function OutcomesView() {
  const [data, setData] = useState(null);
  const [resolving, setResolving] = useState(false);
  const [prices, setPrices] = useState({});      // signal_id -> inline fill price input
  const [busyId, setBusyId] = useState(null);

  useEffect(() => { getOutcomes().then(setData).catch(() => {}); }, []);

  async function resolveNow() {
    setResolving(true);
    try {
      setData(await resolveOutcomes({ horizon: 14 }));
    } catch { /* keep old data */ }
    setResolving(false);
  }

  async function logPaper(signal, kind) {
    setBusyId(signal.signal_id);
    try {
      if (kind === "fill") {
        const raw = (prices[signal.signal_id] ?? "").toString().trim();
        const price = raw === "" ? undefined : Number(raw);
        await paperFill(signal.signal_id, price);
      } else {
        await paperMiss(signal.signal_id);
      }
      setData(await getOutcomes());
    } catch { /* leave row as-is */ }
    setBusyId(null);
  }

  const m = data?.metrics;
  const sched = data?.scheduler || {};
  const schedText = sched.last_run_at
    ? `last pass ${sched.last_run_at.slice(0, 16).replace("T", " ")} (${sched.last_status})`
    : "no resolution pass yet";
  const signals = data?.signals || [];
  const openSignals = signals.filter((s) => !s.status || s.status === "open" || s.status === "no_data");
  const mtm = data?.mtm || {};
  const mtmRows = mtm.rows || {};
  const mtmText = mtm.fetched_at
    ? `live mark-to-market as of ${mtm.fetched_at.slice(0, 16).replace("T", " ")}`
    : "";

  return (
    <div className="outcomes">
      <div className="oc-head">
        <div>
          <h3>Outcome statistics</h3>
          <p className="muted oc-tagline">Measurement, not prediction — what the emitted signals actually did.</p>
        </div>
        <div className="oc-actions">
          <span className="muted oc-sched">{schedText}</span>
          <button onClick={resolveNow} disabled={resolving}>{resolving ? "Resolving…" : "Resolve now"}</button>
        </div>
      </div>

      {data?.stale && (
        <div className="notice">New signals are not resolved yet — press “Resolve now” or wait for the daily pass.</div>
      )}

      {m && m.n_rows === 0 && (
        <p className="muted">No scored signals yet — run a scan, or import history with <code>signaldesk outcomes --backfill</code>.</p>
      )}

      {m && m.n_resolved > 0 && (
        <>
          <div className="oc-cards">
            <StatCard label="Expectancy (net of cost)"
                      value={fmtR(m.expectancy_r)}
                      sub={`95% CI ${fmtR(m.expectancy_ci[0])} … ${fmtR(m.expectancy_ci[1])}`} />
            <StatCard label="Hit rate (TP1+)"
                      value={fmtPct(m.hit_rate)}
                      sub={`95% CI ${fmtPct(m.hit_ci[0])} … ${fmtPct(m.hit_ci[1])}`} />
            <StatCard label="Profit factor" value={fmtPf(m.profit_factor)}
                      sub={`${m.n_resolved} resolved · ${m.blocks} week(s)`} />
            <StatCard label="Median bars to TP1" value={m.median_bars_to_tp1 || "—"}
                      sub={`MFE ${fmtR(m.mean_mfe_r)} · MAE ${fmtR(m.mean_mae_r)}`} />
            <StatCard label="Cost drag" value={`${Number(m.mean_cost_in_r).toFixed(3)}R`}
                      sub={`censored ${fmtPct(m.censored_pct)} · open ${m.n_open}`} />
            <StatCard label="Paper fill rate"
                      value={data.paper.fills + data.paper.misses ? fmtPct(data.paper.fill_rate) : "—"}
                      sub={`gap ${fmtR(data.paper.mean_entry_gap_r)} (n=${data.paper.n_entry_gap}) · funding ${fmtR(data.paper.mean_funding_r)}`} />
          </div>

          <p className="muted oc-variants">
            exit-policy variants — all-in at first target {fmtR(m.expectancy_all_in_r)} · runner-only {fmtR(m.expectancy_runner_r)}
          </p>

          {m.notes?.length > 0 && (
            <ul className="oc-notes">
              {m.notes.map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          )}

          <div className="oc-breakdowns">
            <Breakdown title="market" groups={m.by_market} />
            <Breakdown title="entry mode" groups={m.by_mode} />
          </div>
        </>
      )}

      {signals.length > 0 && (
        <div className="oc-signals">
          <h3>
            {openSignals.length > 0 ? `Signals (${signals.length}, ${openSignals.length} open)` : `Signals (${signals.length})`}
            {mtmText && <span className="muted oc-mtm-when"> · {mtmText}</span>}
          </h3>
          <table className="oc-table">
            <thead>
              <tr>
                <th>symbol</th><th>market</th><th>mode</th><th>entry</th><th>stop</th>
                <th>tp1</th><th>tp2</th><th>status</th><th>r_net</th><th>paper</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.signal_id}>
                  <td>{s.symbol}</td>
                  <td>{s.market}</td>
                  <td>{s.entry_mode}</td>
                  <td>{fmtPx(s.entry)}</td><td>{fmtPx(s.stop)}</td>
                  <td>{fmtPx(s.tp1)}</td><td>{fmtPx(s.tp2)}</td>
                  <td><span className={`status status-${s.status}`}>{s.status}</span></td>
                  <td>
                    {(() => {
                      const isOpen = !s.status || s.status === "open" || s.status === "no_data";
                      const live = isOpen ? mtmRows[s.signal_id] : null;
                      if (live && live.r_unrealized != null) {
                        return (
                          <span
                            className={`mtm-live ${live.r_unrealized >= 0 ? "mtm-pos" : "mtm-neg"}`}
                            title={`mark-to-market @ ${fmtPx(live.price)}`}
                          >
                            {fmtR(live.r_unrealized)}
                          </span>
                        );
                      }
                      return s.r_net == null ? "—" : fmtR(s.r_net);
                    })()}
                  </td>
                  <td>
                    {s.paper
                      ? <span className="muted">{s.paper.event}{s.paper.price != null ? ` @ ${fmtPx(s.paper.price)}` : ""}</span>
                      : (!s.status || s.status === "open" || s.status === "no_data") ? (
                        <span className="paper-actions">
                          <input
                            className="paper-price"
                            placeholder={fmtPx(s.entry)}
                            value={prices[s.signal_id] ?? ""}
                            onChange={(e) => setPrices({ ...prices, [s.signal_id]: e.target.value })}
                          />
                          <button disabled={busyId === s.signal_id} onClick={() => logPaper(s, "fill")}>Fill</button>
                          <button disabled={busyId === s.signal_id} onClick={() => logPaper(s, "miss")}>Miss</button>
                        </span>
                      ) : <span className="muted">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data?.paper?.notes?.length > 0 && (
        <ul className="oc-notes">
          {data.paper.notes.map((n, i) => <li key={i}>{n}</li>)}
        </ul>
      )}
    </div>
  );
}
