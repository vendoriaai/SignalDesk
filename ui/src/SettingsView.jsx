import React, { useEffect, useState } from "react";
import { getSettings, saveSettings, signup, login, logout, syncNow, deleteMyData } from "./api.js";

const SECRET_FIELDS = [
  ["OPENAI_API_KEY", "OpenAI API key"],
  ["ANTHROPIC_API_KEY", "Anthropic API key"],
  ["OPENROUTER_API_KEY", "OpenRouter API key"],
  ["TAVILY_API_KEY", "Tavily search key"],
  ["FRED_API_KEY", "FRED API key"],
  ["ALPHAVANTAGE_API_KEY", "Alpha Vantage key"],
];

export default function SettingsView({ onAuthChange }) {
  const [s, setS] = useState(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [authMsg, setAuthMsg] = useState("");
  const [newSecrets, setNewSecrets] = useState({});

  async function refresh() {
    setS(await getSettings());
  }
  useEffect(() => { refresh().catch(() => {}); }, []);
  if (!s) return <p className="muted">Loading…</p>;

  async function toggleConsent() {
    await saveSettings({ consent_prompts: !s.consent_prompts });
    await refresh();
    onAuthChange?.({ logged_in: false, consent: !s.consent_prompts });
  }

  async function toggleEntryRefinement() {
    await saveSettings({ entry_refinement: !(s.entry_refinement !== false) });
    await refresh();
  }

  async function saveAllSecrets() {
    const secrets = Object.fromEntries(
      Object.entries(newSecrets).filter(([k, v]) => v && !k.startsWith("_"))
    );
    if (Object.keys(secrets).length) await saveSettings({ secrets });
    if (typeof newSecrets._model === "string") await saveSettings({ llm_model: newSecrets._model.trim() });
    setNewSecrets({});
    refresh();
  }

  async function doAuth(fn) {
    setAuthMsg("");
    const r = await fn(email, password);
    if (r.error) setAuthMsg(r.error);
    else setAuthMsg(r.email ? `signed in as ${r.email}` : "signed up — check email confirmation");
    onAuthChange && getSettings().then(() => {});
  }

  return (
    <div className="settings">
      <h3>Settings</h3>

      <section>
        <h4>LLM / data keys</h4>
        <p className="muted">Stored in the OS keychain only; masked below. Leave blank to keep existing.</p>
        {SECRET_FIELDS.map(([key, label]) => (
          <div key={key} className="secret-row">
            <label>{label}</label>
            <span className="muted chip">{s.secrets[key] || "—"}</span>
            <input
              placeholder="new key"
              type="password"
              value={newSecrets[key] || ""}
              onChange={(e) => setNewSecrets({ ...newSecrets, [key]: e.target.value })}
            />
          </div>
        ))}
        <div className="secret-row">
          <label>Model override</label>
          <span className="muted chip">{s.llm_model || "default"}</span>
          <input
            placeholder='e.g. "openrouter/auto" (blank = provider default)'
            value={newSecrets._model || ""}
            onChange={(e) => setNewSecrets({ ...newSecrets, _model: e.target.value })}
          />
        </div>
        <button onClick={saveAllSecrets}>Save keys</button>
      </section>

      <section>
        <h4>Analysis</h4>
        <label className="toggle-row">
          <input type="checkbox" checked={s.entry_refinement !== false} onChange={toggleEntryRefinement} />
          Intraday entry refinement — refine signal entry/stop/TP on 30m/15m/5m/1m bars
        </label>
      </section>

      <section>
        <h4>Privacy</h4>
        <label className="toggle-row">
          <input type="checkbox" checked={!!s.consent_prompts} onChange={toggleConsent} />
          Share prompts & reports with SignalDesk cloud (off = fully local, FR-1.4)
        </label>
      </section>

      <section>
        <h4>Cloud account (Supabase)</h4>
        <p className="muted">History syncs when signed in and consent is on.</p>
        <div className="auth-row">
          <input placeholder="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          <input placeholder="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        <div className="auth-row">
          <button onClick={() => doAuth(signup)}>Sign up</button>
          <button onClick={() => doAuth(login)}>Log in</button>
          <button onClick={async () => { await logout(); onAuthChange?.({ logged_in: false, consent: s.consent_prompts }); setAuthMsg("signed out"); }}>Sign out</button>
          <button onClick={async () => setAuthMsg(JSON.stringify(await syncNow()))}>Sync now</button>
        </div>
        {authMsg && <div className="notice">{authMsg}</div>}
        <button
          className="danger"
          onClick={async () => {
            if (confirm("Delete ALL cloud data for this account? Local history is kept.")) {
              setAuthMsg(JSON.stringify(await deleteMyData()));
            }
          }}
        >
          Delete my cloud data
        </button>
      </section>
    </div>
  );
}
