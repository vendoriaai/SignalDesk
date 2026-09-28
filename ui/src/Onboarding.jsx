import React, { useState } from "react";
import { saveSettings, postChat, getRun, wsUrl } from "./api.js";

/**
 * First-run wizard (roadmap Phase 4.26):
 *  1. pick LLM (BYOK or local Ollama)
 *  2. optional Supabase account
 *  3. consent screen
 *  4. run demo scan
 */
export default function Onboarding({ onFinish }) {
  const [step, setStep] = useState(0);
  const [llmChoice, setLlmChoice] = useState("byok"); // byok | openrouter | ollama | none
  const [llmKey, setLlmKey] = useState("");
  const [llmModel, setLlmModel] = useState("");
  const [sbUrl, setSbUrl] = useState("");
  const [sbKey, setSbKey] = useState("");
  const [consent, setConsent] = useState(false);
  const [demoRun, setDemoRun] = useState(null);
  const [demoMsg, setDemoMsg] = useState("");
  const [busy, setBusy] = useState(false);

  async function doStep0Next() {
    const secrets = {};
    if (llmChoice === "byok" && llmKey.trim()) {
      secrets.OPENAI_API_KEY = llmKey.trim();
      secrets.ANTHROPIC_API_KEY = llmKey.trim();
    }
    if (llmChoice === "openrouter" && llmKey.trim()) secrets.OPENROUTER_API_KEY = llmKey.trim();
    if (llmChoice === "ollama") secrets._skip = true; // local Ollama needs no key
    delete secrets._skip;
    if (Object.keys(secrets).length) await saveSettings({ secrets });
    if (llmChoice === "openrouter" && llmModel.trim()) await saveSettings({ llm_model: llmModel.trim() });
    setStep(1);
  }

  async function doStep1Next() {
    const secrets = {};
    if (sbUrl.trim()) secrets._SUPABASE_URL = sbUrl.trim(); // URL/anon key aren't secrets; store via settings
    if (sbKey.trim()) secrets.SUPABASE_ANON_KEY = sbKey.trim();
    if (Object.keys(secrets).length) {
      delete secrets._SUPABASE_URL;
      await saveSettings({ secrets: { SUPABASE_ANON_KEY: sbKey.trim() } });
    }
    if (sbUrl.trim()) await saveSettings({ supabase_configured: true, supabase_url: sbUrl.trim() });
    setStep(2);
  }

  async function doStep2Next() {
    await saveSettings({ consent_prompts: consent });
    setStep(3);
  }

  async function runDemo() {
    setBusy(true);
    setDemoMsg("running demo scan… (offline, a few seconds)");
    const r = await postChat("scan crypto", true);
    if (r.error) { setDemoMsg(r.error); setBusy(false); return; }
    const ws = new WebSocket(wsUrl(r.run_id));
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.kind === "done") {
        ws.close();
        getRun(r.run_id).then((d) => {
          const sigs = d.report_json?.signals || [];
          setDemoMsg(
            `demo complete: ${sigs.length} signals, score top = ${sigs[0] ? sigs[0].score : "n/a"}. ` +
            `Open Chat to see the full cited report.`
          );
          setBusy(false);
        });
      }
    };
  }

  async function finish() {
    const { api } = await import("./api.js");
    await api("/api/onboarding/finish", { method: "POST" });
    onFinish();
  }

  return (
    <div className="onboarding">
      <div className="onboard-card">
        <h2>Welcome to SignalDesk</h2>
        <div className="steps">step {step + 1} / 4</div>

        {step === 0 && (
          <div>
            <h4>1 · Pick your LLM</h4>
            <label><input type="radio" checked={llmChoice === "byok"} onChange={() => setLlmChoice("byok")} /> BYOK — paste an OpenAI/Anthropic key</label>
            {llmChoice === "byok" && <input type="password" placeholder="sk-..." value={llmKey} onChange={(e) => setLlmKey(e.target.value)} />}
            <label><input type="radio" checked={llmChoice === "openrouter"} onChange={() => setLlmChoice("openrouter")} /> OpenRouter — single key, many models</label>
            {llmChoice === "openrouter" && (
              <div>
                <input type="password" placeholder="sk-or-..." value={llmKey} onChange={(e) => setLlmKey(e.target.value)} />
                <input type="text" placeholder='model (optional, e.g. "openrouter/auto")' value={llmModel} onChange={(e) => setLlmModel(e.target.value)} />
              </div>
            )}
            <label><input type="radio" checked={llmChoice === "ollama"} onChange={() => setLlmChoice("ollama")} /> Local model via Ollama (no key needed)</label>
            <label><input type="radio" checked={llmChoice === "none"} onChange={() => setLlmChoice("none")} /> Skip — rules-only planner is fine for now</label>
            <div className="wizard-btns"><button onClick={doStep0Next}>Next</button></div>
          </div>
        )}

        {step === 1 && (
          <div>
            <h4>2 · Cloud sync (optional)</h4>
            <p className="muted">Connect Supabase to sync history across machines. Requires signup; you can do this later in Settings.</p>
            <input placeholder="SUPABASE_URL" value={sbUrl} onChange={(e) => setSbUrl(e.target.value)} />
            <input placeholder="SUPABASE_ANON_KEY" value={sbKey} onChange={(e) => setSbKey(e.target.value)} />
            <div className="wizard-btns">
              <button onClick={() => setStep(2)}>Skip</button>
              <button onClick={doStep1Next}>Next</button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div>
            <h4>3 · Privacy & consent</h4>
            <label className="toggle-row">
              <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
              I agree to share prompts and reports with SignalDesk cloud (helps improve prompts). Off = everything stays local.
            </label>
            <div className="wizard-btns"><button onClick={doStep2Next}>Continue</button></div>
          </div>
        )}

        {step === 3 && (
          <div>
            <h4>4 · Run the demo scan</h4>
            <p className="muted">Runs the full agent on a synthetic offline market — verifies your install end-to-end.</p>
            <button onClick={runDemo} disabled={busy}>{busy ? "…" : "Run demo scan"}</button>
            {demoMsg && <div className="notice">{demoMsg}</div>}
            <div className="wizard-btns">
              <button onClick={finish} disabled={busy}>{demoMsg && demoMsg.startsWith("demo complete") ? "Finish" : "Skip to app"}</button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
