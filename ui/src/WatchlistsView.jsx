import React, { useEffect, useState } from "react";
import { listWatchlists, createWatchlist, patchWatchlist, scanWatchlist } from "./api.js";

export default function WatchlistsView() {
  const [lists, setLists] = useState([]);
  const [name, setName] = useState("");
  const [symbols, setSymbols] = useState("");
  const [market, setMarket] = useState("crypto");
  const [msg, setMsg] = useState("");

  async function refresh() {
    setLists(await listWatchlists());
  }
  useEffect(() => { refresh().catch(() => {}); }, []);

  async function create(e) {
    e.preventDefault();
    const syms = symbols.split(/[\s,]+/).filter(Boolean).map((s) => s.toUpperCase());
    if (!name.trim() || syms.length === 0) return;
    await createWatchlist(name.trim().toLowerCase(), syms, market);
    setName(""); setSymbols("");
    refresh();
  }

  async function removeSymbol(wlName, sym) {
    await patchWatchlist(wlName, [], [sym]);
    refresh();
  }

  async function rescan(wlName) {
    setMsg(`scanning ${wlName}…`);
    await scanWatchlist(wlName, false);
    setMsg(`scan for '${wlName}' started — see History when it finishes`);
  }

  return (
    <div className="watchlists">
      <h3>Watchlists</h3>
      <form className="wl-form" onSubmit={create}>
        <input placeholder="name (e.g. growth)" value={name} onChange={(e) => setName(e.target.value)} />
        <input className="wl-syms" placeholder="symbols: BTCUSD ETHUSD" value={symbols} onChange={(e) => setSymbols(e.target.value)} />
        <select value={market} onChange={(e) => setMarket(e.target.value)}>
          <option value="crypto">crypto</option>
          <option value="forex">forex</option>
          <option value="metals">metals</option>
        </select>
        <button>Add</button>
      </form>
      {msg && <div className="notice">{msg}</div>}
      {lists.length === 0 && <p className="muted">No watchlists yet.</p>}
      {lists.map((wl) => (
        <div key={wl.name} className="wl-card">
          <div className="wl-head">
            <strong>{wl.name}</strong> <span className="muted">[{wl.market}]</span>
            <button className="wl-scan" onClick={() => rescan(wl.name)}>Scan now</button>
          </div>
          <div className="wl-body">
            {wl.symbols.map((s) => (
              <span key={s} className="chip" onClick={() => removeSymbol(wl.name, s)} title="click to remove">
                {s} ×
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
