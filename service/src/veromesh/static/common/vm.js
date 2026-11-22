/* VeroMesh screens: what the steward screen and the Administration area share. Plain JavaScript,
   no build step. Every value from the API is written with textContent, never as HTML. Sign-in:
   OpenID Connect authorization code with PKCE, in the browser; the access token stays in this
   tab's sessionStorage, so one sign-in serves both screens. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
};
const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* private window */ } },
  del(k) { try { sessionStorage.removeItem(k); } catch { /* ignore */ } },
  keep(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
  kept(k) { try { return localStorage.getItem(k); } catch { return null; } },
};

const state = { config: null };

// ------------------------------------------------------------------ messages

function say(text, kind) {
  const m = $("#message");
  m.textContent = text;
  m.className = "message " + (kind || "");
  m.hidden = !text;
  if (kind === "ok") setTimeout(() => { if (m.textContent === text) m.hidden = true; }, 5000);
}

// ------------------------------------------------------------------ sign-in (PKCE)

const b64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const PAGE = document.body.dataset.page || "/steward/"; // this screen's path: its sign-in redirect
const redirectUri = () => location.origin + PAGE;

async function signIn() {
  const c = state.config;
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(48)));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const st = b64url(crypto.getRandomValues(new Uint8Array(16)));
  store.set("vm.verifier", verifier);
  store.set("vm.state", st);
  const q = new URLSearchParams({
    response_type: "code", client_id: c.clientId, redirect_uri: redirectUri(), scope: c.scope,
    state: st, code_challenge: challenge, code_challenge_method: "S256",
  });
  location.assign(c.authorizationEndpoint + "?" + q.toString());
}

async function finishSignIn(params) {
  if (params.get("state") !== store.get("vm.state")) throw new Error("Sign-in answer does not match this tab; sign in again.");
  const body = new URLSearchParams({
    grant_type: "authorization_code", client_id: state.config.clientId, code: params.get("code"),
    redirect_uri: redirectUri(), code_verifier: store.get("vm.verifier") || "",
  });
  const r = await fetch(state.config.tokenEndpoint, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
  const t = await r.json().catch(() => ({}));
  if (!r.ok || !t.access_token) throw new Error("Sign-in failed: " + (t.error_description || t.error || r.status));
  store.set("vm.token", t.access_token);
  store.set("vm.expires", String(Date.now() + (Number(t.expires_in) || 3600) * 1000 - 60000));
  store.del("vm.verifier");
  store.del("vm.state");
  history.replaceState(null, "", PAGE);
}

function token() {
  const t = store.get("vm.token");
  return t && Number(store.get("vm.expires") || 0) > Date.now() ? t : null;
}

// ------------------------------------------------------------------ API

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const t = token();
  if (t) headers.Authorization = "Bearer " + t;
  const r = await fetch(path, { method: opts.method || "GET", headers, body: opts.body === undefined ? undefined : JSON.stringify(opts.body) });
  if (r.status === 401 && !state.config.disabled) {
    store.del("vm.token");
    await signIn();
    return new Promise(() => {});
  }
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : `${r.status} ${r.statusText}`);
  }
  return data;
}

const dev = () => state.config && state.config.disabled;

// ------------------------------------------------------------------ licence banner

async function licenceBanner() {
  const b = $("#licence-banner");
  if (!b) return;
  try {
    const st = await api("/api/v1/licence/status");
    b.textContent = st.message || "";
    b.className = "banner " + (st.stage === "read-only" ? "bad" : "warn");
    b.hidden = !st.message || st.stage === "licensed";
  } catch { b.hidden = true; }
}

const num = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString());
const count = (n, one, many) => `${num(n)} ${Number(n) === 1 ? one : many}`;
const when = (iso) => { try { return new Date(iso).toLocaleString(); } catch { return iso; } };
