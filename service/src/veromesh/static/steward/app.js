/* VeroMesh steward screen. Plain JavaScript, no build step. Every value from the API is written
   with textContent, never as HTML. Sign-in: OpenID Connect authorization code with PKCE, in the
   browser; the access token stays in this tab's sessionStorage. */
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

const state = { config: null, me: null, person: null, roles: [], steward: false, names: {} };

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
const redirectUri = () => location.origin + "/steward/";

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
  history.replaceState(null, "", "/steward/");
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
const withPerson = (path) => (dev() && state.person ? path + (path.includes("?") ? "&" : "?") + "person=" + encodeURIComponent(state.person) : path);

// ------------------------------------------------------------------ names

async function loadNames() {
  const types = ["actions", "agents", "people", "roles"];
  const lists = await Promise.all(types.map((t) => api(`/api/v1/${t}`).catch(() => [])));
  types.forEach((t, i) => { for (const e of lists[i]) state.names[`${t}:${e.id}`] = e.name || e.id; });
}
const nameOf = (type, id) => (id ? state.names[`${type}:${id}`] || id : "");
const when = (iso) => { try { return new Date(iso).toLocaleString(); } catch { return iso; } };
const money = (amount, cur) => (amount ? `${Number(amount).toLocaleString()} ${cur || ""}`.trim() : "");

// ------------------------------------------------------------------ waiting for you

const ASK = {
  answer: { label: "Your answer", buttons: [["Approve", "approved", "primary"], ["Reject", "rejected", "danger"]] },
  escalation: { label: "Above the limit", buttons: [["Approve", "approved", "primary"]] },
  stop: { label: "Allowed", buttons: [["Stop", "rejected", "danger"]] },
};

async function showInbox() {
  const box = $("#inbox");
  box.replaceChildren(el("p", "lead", "Loading…"));
  let items;
  try { items = await api(withPerson("/api/v1/decisions/inbox")); } catch (e) { box.replaceChildren(el("div", "empty", e.message)); return; }
  $("#inbox-count").textContent = items.filter((i) => i.ask !== "stop").length || "";
  if (!items.length) { box.replaceChildren(el("div", "empty", "Nothing is waiting for you.")); return; }
  box.replaceChildren(...items.map(card));
}

function card(item) {
  const d = item.decision;
  const ask = ASK[item.ask];
  const node = $("#card").content.firstElementChild.cloneNode(true);
  const kind = node.querySelector(".kind");
  kind.textContent = ask.label;
  kind.classList.add(item.ask);
  node.querySelector("h3").textContent = nameOf("actions", d.action) + (d.note ? ` · ${d.note}` : "");
  node.querySelector(".amount").textContent = money(d.amount, d.currency);
  node.querySelector(".meta").textContent =
    `${nameOf("agents", d.agent)}, acting for ${nameOf("people", d.principal)} · asked ${when(d.recordedAt)}` +
    (item.asRole ? ` · you answer as ${nameOf("roles", item.asRole)}` : "");
  const reasons = node.querySelector(".reasons");
  const lines = [...(d.reasons || [])];
  if (item.ask === "escalation" && d.basis && d.basis.escalateTo) lines.push("Approving takes responsibility for an action above the agent's authority.");
  for (const r of lines) reasons.append(el("li", null, r));
  const note = node.querySelector(".note");
  const buttons = node.querySelector(".buttons");
  for (const [label, verdict, style] of ask.buttons) {
    const b = el("button", style, label);
    b.type = "button";
    b.addEventListener("click", async () => {
      node.querySelectorAll("button").forEach((x) => { x.disabled = true; });
      try {
        const body = { verdict, note: note.value || null };
        if (item.asRole) body.role = item.asRole;
        if (dev()) body.person = state.person;
        const r = await api(`/api/v1/decisions/${d.id}/overrides`, { method: "POST", body });
        say(`${label === "Stop" ? "Stopped" : verdict === "approved" ? "Approved" : "Rejected"}: ${nameOf("actions", d.action)} ${money(d.amount, d.currency)}. It now stands ${r.status.status}.`, "ok");
        showInbox();
      } catch (e) {
        say(e.message, "error");
        node.querySelectorAll("button").forEach((x) => { x.disabled = false; });
      }
    });
    buttons.append(b);
  }
  return node;
}

// ------------------------------------------------------------------ facts to confirm

async function showFacts() {
  const box = $("#facts");
  box.replaceChildren(el("p", "lead", "Loading…"));
  const type = $("#facts-type").value;
  let rows;
  try { rows = await api("/api/v1/attestations?limit=500" + (type ? "&type=" + encodeURIComponent(type) : "")); } catch (e) { box.replaceChildren(el("div", "empty", e.message)); return; }
  if (!type) $("#facts-count").textContent = rows.length || "";
  const typeSel = $("#facts-type");
  for (const t of [...new Set(rows.map((r) => r.type))]) {
    if (![...typeSel.options].some((o) => o.value === t)) typeSel.append(new Option(t, t));
  }
  if (!rows.length) { box.replaceChildren(el("div", "empty", "Every fact is attested.")); return; }
  box.replaceChildren(...rows.map(factCard));
}

function factCard(row) {
  const node = el("article", "card");
  const head = el("header");
  const k = el("span", "kind pending", row.type);
  head.append(k, el("h3", null, row.name || row.id));
  node.append(head);
  node.append(el("p", "meta", `${row.id}` + (row.ownerRole ? ` · owner: ${nameOf("roles", row.ownerRole)}` : "")));
  const chips = el("div", "fields");
  const open = [];
  for (const [f, s] of Object.entries(row.fields || {})) {
    chips.append(el("span", "chip " + (s === "attested" ? "ok" : s === "disputed" || s === "expired" ? "bad" : ""), `${f}: ${s}`));
    if (s !== "attested") open.push(f);
  }
  node.append(chips);
  const answer = el("div", "answer");
  const note = el("input", "note");
  note.type = "text";
  note.placeholder = "Note (optional)";
  note.setAttribute("aria-label", "Note");
  const buttons = el("div", "buttons");
  for (const [label, verdict, style] of [["Attest", "attested", "primary"], ["Dispute", "disputed", "danger"]]) {
    const b = el("button", style, label);
    b.type = "button";
    b.addEventListener("click", async () => {
      const role = $("#attest-role").value;
      if (!role) { say("Choose the role you attest as.", "error"); return; }
      b.disabled = true;
      try {
        const body = { verdict, role, fields: open, note: note.value || null };
        if (dev()) body.attester = state.person;
        const r = await api(`/api/v1/${row.type}/${encodeURIComponent(row.id)}/attestations`, { method: "POST", body });
        say(`${verdict === "attested" ? "Attested" : "Disputed"}: ${row.name || row.id}.` + (r.warnings && r.warnings.length ? " " + r.warnings.join(" ") : ""), "ok");
        showFacts();
      } catch (e) { say(e.message, "error"); b.disabled = false; }
    });
    buttons.append(b);
  }
  answer.append(note, buttons);
  node.append(answer);
  return node;
}

// ------------------------------------------------------------------ decisions

async function showLog() {
  const box = $("#log");
  box.replaceChildren(el("p", "lead", "Loading…"));
  try {
    const v = await api("/api/v1/decisions/verify");
    const chip = $("#chain");
    chip.textContent = v.ok ? `Chain intact · ${v.entries} entries` : `Chain broken at entry ${v.brokenAtSeq}: ${v.problem}`;
    chip.className = "chip " + (v.ok ? "ok" : "bad");
    const all = await api("/api/v1/decisions?kind=decision&limit=1000");
    const recent = all.slice(-100).reverse();
    if (!recent.length) { box.replaceChildren(el("div", "empty", "No decisions yet.")); return; }
    const statuses = await Promise.all(recent.map((d) => api(`/api/v1/decisions/${d.id}/status`).catch(() => null)));
    const table = el("table");
    const hr = el("tr");
    for (const h of ["When", "Agent", "Action", "Amount", "Check", "Now", "Done"]) hr.append(el("th", null, h));
    table.append(hr);
    recent.forEach((d, i) => {
      const s = statuses[i] || {};
      const tr = el("tr");
      tr.append(el("td", null, when(d.recordedAt)), el("td", null, nameOf("agents", d.agent)), el("td", null, nameOf("actions", d.action) + (d.note ? ` · ${d.note}` : "")));
      tr.append(el("td", "num", money(d.amount, d.currency)));
      const c1 = el("td"); c1.append(el("span", "kind " + d.outcome, d.outcome)); tr.append(c1);
      const c2 = el("td"); if (s.status) c2.append(el("span", "kind " + s.status, s.status)); tr.append(c2);
      tr.append(el("td", null, s.done || ""));
      table.append(tr);
    });
    box.replaceChildren(table);
  } catch (e) { box.replaceChildren(el("div", "empty", e.message)); }
}

// ------------------------------------------------------------------ measures

async function showMeasures() {
  const box = $("#measures");
  try {
    const m = await api("/api/v1/decisions/measures");
    const pct = (v) => (v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`);
    const tile = (label, value, detail) => { const t = el("div", "tile"); t.append(el("div", "label", label), el("div", "value", value), el("div", "detail", detail)); return t; };
    box.replaceChildren(
      tile("Decisions", String(m.decisions), `${m.byOutcome.allow} allowed · ${m.byOutcome.consult} consulted · ${m.byOutcome.refuse} refused · ${m.carriedOut} carried out`),
      tile("Override rate", pct(m.overrideRate.value), `${m.overrideRate.overruled} of ${m.overrideRate.of} overruled by a person`),
      tile("Consultation precision", pct(m.consultationPrecision.value), `${m.consultationPrecision.rejected} rejected of ${m.consultationPrecision.answered} answered · ${m.consultationPrecision.pending} waiting`),
      tile("Within delegation", pct(m.delegationExtended.withinDelegation), `${m.delegationExtended.escalationsApproved} escalations approved by the principal`),
    );
  } catch (e) { box.replaceChildren(el("div", "empty", e.message)); }
}

// ------------------------------------------------------------------ tabs and start

const SHOW = { inbox: showInbox, facts: showFacts, log: showLog, measures: showMeasures };

function openTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tab").forEach((s) => { s.hidden = s.id !== "tab-" + name; });
  SHOW[name]();
}

async function choosePerson(pid) {
  state.person = pid;
  store.keep("vm.actas", pid);
  const p = await api(`/api/v1/people/${encodeURIComponent(pid)}`).catch(() => null);
  state.roles = (p && p.rolesFilled) || [];
  fillRoles();
  $("#who").textContent = `${nameOf("people", pid)} · ${state.roles.map((r) => nameOf("roles", r)).join(", ") || "no roles"}`;
}

function fillRoles() {
  const sel = $("#attest-role");
  sel.replaceChildren(...state.roles.map((r) => new Option(nameOf("roles", r), r)));
}

async function start() {
  try {
    state.config = await (await fetch("/api/v1/sign-in/config")).json();
    if (!state.config.disabled) {
      if (state.config.problem) { say(state.config.problem, "error"); return; }
      const params = new URLSearchParams(location.search);
      if (params.get("code")) await finishSignIn(params);
      else if (params.get("error")) throw new Error("Sign-in failed: " + (params.get("error_description") || params.get("error")));
      if (!token()) { await signIn(); return; }
    }
    await loadNames();
    if (dev()) {
      $("#devbar").hidden = false;
      state.steward = true;
      const sel = $("#actas");
      const people = Object.keys(state.names).filter((k) => k.startsWith("people:")).map((k) => k.slice(7));
      sel.replaceChildren(...people.map((p) => new Option(nameOf("people", p), p)));
      const kept = store.kept("vm.actas");
      sel.value = people.includes(kept) ? kept : people[0] || "";
      sel.addEventListener("change", async () => { await choosePerson(sel.value); openTab(document.querySelector('.tabs [aria-selected="true"]').dataset.tab); });
      if (sel.value) await choosePerson(sel.value);
    } else {
      state.me = await api("/api/v1/me");
      state.steward = state.me.roles.includes("steward") || state.me.roles.includes("admin");
      if (!state.me.person) { say(state.me.problem || "Your sign-in does not match a person in the model.", "error"); }
      state.person = state.me.person && state.me.person.id;
      state.roles = (state.me.person && state.me.person.rolesFilled) || [];
      fillRoles();
      $("#who").textContent = (state.me.person && state.me.person.name) || state.me.name || state.me.subject;
      const so = $("#signout");
      so.hidden = false;
      so.addEventListener("click", () => { store.del("vm.token"); store.del("vm.expires"); location.assign("/steward/"); });
    }
    document.querySelectorAll("[data-steward]").forEach((b) => { b.hidden = !state.steward; });
    document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => openTab(b.dataset.tab)));
    $("#facts-type").addEventListener("change", showFacts);
    openTab("inbox");
    if (state.steward) api("/api/v1/attestations?limit=500").then((r) => { $("#facts-count").textContent = r.length || ""; }).catch(() => {});
  } catch (e) {
    say(e.message, "error");
  }
}

start();
