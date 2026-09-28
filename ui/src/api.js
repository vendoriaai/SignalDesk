export async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  return resp.json();
}

export const getAuthStatus = () => api("/api/auth/status");
export const postChat = (prompt, demo = false) => api("/api/chat", { method: "POST", body: { prompt, demo } });
export const getRun = (id) => api(`/api/runs/${id}`);
export const listRuns = () => api("/api/runs");
export const listWatchlists = () => api("/api/watchlists");
export const createWatchlist = (name, symbols, market) =>
  api("/api/watchlists", { method: "POST", body: { name, symbols, market } });
export const patchWatchlist = (name, add, remove) =>
  api(`/api/watchlists/${name}/symbols`, { method: "POST", body: { add, remove } });
export const scanWatchlist = (name, demo) =>
  api(`/api/watchlists/${name}/scan?demo=${demo ? "true" : "false"}`, { method: "POST" });
export const getSettings = () => api("/api/settings");
export const saveSettings = (body) => api("/api/settings", { method: "POST", body });
export const signup = (email, password) => api("/api/auth/signup", { method: "POST", body: { email, password } });
export const login = (email, password) => api("/api/auth/login", { method: "POST", body: { email, password } });
export const logout = () => api("/api/auth/logout", { method: "POST" });
export const syncNow = () => api("/api/sync", { method: "POST" });
export const deleteMyData = () => api("/api/auth/delete-my-data", { method: "POST" });
export const getOnboarding = () => api("/api/onboarding");
export const finishOnboarding = () => api("/api/onboarding/finish", { method: "POST" });
export const getUpdateCheck = () => api("/api/update-check");
export const getVersion = () => api("/api/version");

export function wsUrl(runId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/runs/${runId}`;
}
