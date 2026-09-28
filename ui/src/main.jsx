import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import ChatView from "./ChatView.jsx";
import HistoryView from "./HistoryView.jsx";
import WatchlistsView from "./WatchlistsView.jsx";
import SettingsView from "./SettingsView.jsx";
import Onboarding from "./Onboarding.jsx";
import { getAuthStatus, getOnboarding, getUpdateCheck } from "./api.js";

export default function App() {
  const [view, setView] = useState("chat");
  const [auth, setAuth] = useState({ logged_in: false, consent: false });
  const [onboarded, setOnboarded] = useState(null); // null = loading
  const [update, setUpdate] = useState(null);

  useEffect(() => {
    getAuthStatus().then(setAuth).catch(() => {});
    getOnboarding().then((o) => setOnboarded(o.done)).catch(() => setOnboarded(true));
    // update manifest: manual-download notice only (v1 policy)
    getUpdateCheck().then((u) => u.update_available && setUpdate(u)).catch(() => {});
  }, []);

  if (onboarded === null) return null;
  if (!onboarded) {
    return <Onboarding onFinish={() => setOnboarded(true)} />;
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          📡 <strong>SignalDesk</strong>
        </div>
        <nav>
          <button className={view === "chat" ? "active" : ""} onClick={() => setView("chat")}>Chat</button>
          <button className={view === "history" ? "active" : ""} onClick={() => setView("history")}>History</button>
          <button className={view === "watchlists" ? "active" : ""} onClick={() => setView("watchlists")}>Watchlists</button>
          <button className={view === "settings" ? "active" : ""} onClick={() => setView("settings")}>Settings</button>
        </nav>
        <div className="sidebar-foot">
          <span className={`dot ${auth.logged_in ? "on" : "off"}`} />
          {auth.logged_in ? "cloud linked" : "local-only"}
        </div>
      </aside>
      <main className="main">
        {update && (
          <div className="update-banner">
            Update available: v{update.latest} (you have v{update.current}) —{" "}
            <a href={update.url} target="_blank" rel="noreferrer">download</a>
            {update.notes ? ` — ${update.notes}` : ""}
          </div>
        )}
        {view === "chat" && <ChatView />}
        {view === "history" && <HistoryView />}
        {view === "watchlists" && <WatchlistsView />}
        {view === "settings" && <SettingsView onAuthChange={setAuth} />}
      </main>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
