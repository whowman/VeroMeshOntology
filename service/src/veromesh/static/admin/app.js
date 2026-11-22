/* VeroMesh Administration: the install's licence and its usage reports. Administrators only.
   Plain JavaScript, no build step; sign-in, the API helper and the licence banner are in
   /common/vm.js. Every value from the API is written with textContent, never as HTML. */
"use strict";

Object.assign(state, { me: null });

const STAGE = {
  unlicensed: ["No licence", "warn"],
  licensed: ["Licensed", "ok"],
  warning: ["Ending soon", "warn"],
  grace: ["In grace", "warn"],
  "read-only": ["Read-only", "bad"],
};
const TIER = { small: ["Small", "5,000"], medium: ["Medium", "25,000"], large: ["Large", "100,000"], "extra-large": ["Extra large", "500,000 and up"] };
const tierText = (t) => (TIER[t] ? `${TIER[t][0]}, ${TIER[t][1]}` : t || "");
const day = (iso) => (iso ? String(iso).slice(0, 10) : "–");

// ------------------------------------------------------------------ licence

async function showLicence() {
  const tiles = $("#licence-tiles");
  try {
    const o = await api("/api/v1/licence");
    const st = o.status;
    const lic = st.licence;
    const tile = (label, value, detail, cls) => { const t = el("div", "tile"); const v = el("div", "value " + (cls || ""), value); t.append(el("div", "label", label), v, el("div", "detail", detail)); return t; };
    const [stageName, cls] = STAGE[st.stage] || [st.stage, ""];
    tiles.replaceChildren(
      tile("Stage", stageName, st.message || (lic ? `Until ${lic.validUntil}` : ""), cls),
      tile("Customer", lic ? lic.customer : o.install.customer || "–", lic ? `Licence ${lic.id} · ${lic.environment}` : "No licence loaded"),
      tile("Tier", lic ? (TIER[lic.tier] || [lic.tier])[0] : "–", lic ? `${(TIER[lic.tier] || ["", ""])[1]} identities in scope; never a limit` : "From the licence"),
      tile("Read-only from", st.readOnlyFrom ? day(st.readOnlyFrom) : "–", lic ? `Ends ${lic.validUntil}, then ${lic.graceDays} days of grace` : "30 days after the first start, without a licence"),
    );
    showIds(o.install);
    showLicences(o.licences);
  } catch (e) { tiles.replaceChildren(el("div", "empty", e.message)); }
}

function showIds(inst) {
  const box = $("#install-ids");
  const row = (label, value) => {
    const r = el("div", "id-row");
    const code = el("code", "mono", value);
    const copy = el("button", "secondary", "Copy");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(value); say(`${label} copied.`, "ok"); } catch { say("Copy it by hand: the browser refused.", "error"); }
    });
    r.append(el("span", "label", label), code, copy);
    return r;
  };
  box.replaceChildren(row("Install ID", inst.id), row("Public key", inst.usageKey));
}

function showLicences(list) {
  const box = $("#licences");
  if (!list.length) { box.replaceChildren(el("div", "empty", "No licence held yet.")); return; }
  const table = el("table");
  const hr = el("tr");
  for (const h of ["Licence", "From", "Until", "Tier", "Where from", ""]) hr.append(el("th", null, h));
  table.append(hr);
  for (const x of list) {
    const l = x.licence || {};
    const tr = el("tr");
    tr.append(el("td", "nowrap", l.id || "?"), el("td", "nowrap", l.validFrom || ""), el("td", "nowrap", l.validUntil || ""), el("td", null, tierText(l.tier)), el("td", null, x.origin === "secret" ? "Install setting" : "Loaded here"));
    const s = el("td");
    if (x.problem) s.append(el("span", "chip bad", "Not used"), el("div", "meta", x.problem));
    else s.append(el("span", "chip ok", "Genuine"));
    tr.append(s);
    table.append(tr);
  }
  box.replaceChildren(table);
}

async function loadLicence(ev) {
  ev.preventDefault();
  const f = $("#licence-file").files[0];
  if (!f) { say("Choose the licence file first.", "error"); return; }
  try {
    const doc = JSON.parse(await f.text());
    const st = await api("/api/v1/licence", { method: "PUT", body: doc });
    say(`Licence loaded. ${st.message || "This install is licensed."}`, "ok");
    showLicence();
    licenceBanner();
  } catch (e) { say(e.message, "error"); }
}

// ------------------------------------------------------------------ usage (administrators)


async function showUsage() {
  const tiles = $("#usage-tiles");
  try {
    const o = await api("/api/v1/usage");
    const cur = o.current;
    const ids = cur.identities;
    const peak = ids.peak;
    const tile = (label, value, detail) => { const t = el("div", "tile"); t.append(el("div", "label", label), el("div", "value", value), el("div", "detail", detail)); return t; };
    const share = ids.shareOfTier === null ? "–" : `${(Number(ids.shareOfTier) * 100).toFixed(1)}%`;
    tiles.replaceChildren(
      tile("Identities in scope", num(peak && peak.total), peak ? `${count(peak.people, "person", "people")} · ${count(peak.agents, "agent", "agents")} · ${count(peak.serviceAccounts, "service account", "service accounts")} · peak of ${cur.month} so far` : "Not counted yet"),
      tile("Share of tier", share, o.tier ? `${o.tier.name}, ${num(o.tier.identities)} identities` : "No tier set (VEROMESH_LICENSED_TIER)"),
      tile("Decision checks", num(cur.shown.decisionLog.decision || 0), `${count(cur.shown.decisionLog.override || 0, "override", "overrides")} · ${count(cur.shown.decisionLog.outcome || 0, "outcome", "outcomes")} in ${cur.month}`),
      tile("Install", o.install.customer || o.install.id, `${o.install.hosting} · version ${o.install.version} · ${o.install.id}`),
    );
    showUsageReports(o.reports);
    fillUsageForm(o.settings);
  } catch (e) { tiles.replaceChildren(el("div", "empty", e.message)); }
}

function showUsageReports(reports) {
  const box = $("#usage-reports");
  if (!reports.length) { box.replaceChildren(el("div", "empty", "No report yet: a month gets its report once it has ended.")); return; }
  const table = el("table");
  const hr = el("tr");
  for (const h of ["Month", "Made", "Hash", "Sent", ""]) hr.append(el("th", null, h));
  table.append(hr);
  for (const r of reports.slice().reverse()) {
    const tr = el("tr");
    tr.append(el("td", "nowrap", r.month), el("td", null, when(r.createdAt)), el("td", "mono", r.hash.slice(0, 16) + "…"));
    const sent = el("td");
    if (r.delivery && r.delivery.ok) sent.append(el("span", "chip ok", `Sent ${when(r.delivery.attemptedAt)}`));
    else if (r.delivery) sent.append(el("span", "chip bad", "Not sent, will retry"), el("div", "meta", `${when(r.delivery.attemptedAt)}: ${r.delivery.detail}`));
    else sent.append(el("span", "chip", "Not sent"));
    tr.append(sent);
    const act = el("td");
    const dl = el("button", "link", "Download");
    dl.type = "button";
    dl.addEventListener("click", () => downloadReport(r.month));
    const send = el("button", "link", "Send now");
    send.type = "button";
    send.addEventListener("click", async () => {
      try { const s = await api(`/api/v1/usage/reports/${r.month}/send`, { method: "POST" }); say(s.ok ? `${r.month} sent: ${s.detail}` : `${r.month} not sent: ${s.detail}`, s.ok ? "ok" : "error"); showUsage(); }
      catch (e) { say(e.message, "error"); }
    });
    act.append(dl, send);
    tr.append(act);
    table.append(tr);
  }
  box.replaceChildren(table);
}

async function downloadReport(month) {
  try {
    const doc = await api(`/api/v1/usage/reports/${month}`);
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `veromesh-usage-${doc.report.install.id}-${month}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  } catch (e) { say(e.message, "error"); }
}

function fillUsageForm(s) {
  const m = s.mail;
  $("#u-tz").value = s.timeZone;
  $("#u-adapter").value = m.adapter;
  $("#u-to").value = m.to;
  $("#u-from").value = m.from;
  $("#u-host").value = m.smtp.host;
  $("#u-port").value = String(m.smtp.port);
  $("#u-security").value = m.smtp.security;
  $("#u-user").value = m.smtp.username;
  $("#u-path").value = m.sendmail.path;
  $("#u-tenant").value = m.m365.tenantId;
  $("#u-client").value = m.m365.clientId;
  $("#u-mailbox").value = m.m365.mailbox;
  const set = (b) => (b ? "set" : "not set");
  $("#u-secrets").textContent = `Secrets come from the install's environment, never this page: SMTP password ${set(s.secrets.smtpPassword)} (VEROMESH_USAGE_SMTP_PASSWORD), Microsoft 365 client secret ${set(s.secrets.m365ClientSecret)} (VEROMESH_USAGE_M365_CLIENT_SECRET).`;
  showAdapterFields();
}

function showAdapterFields() {
  const a = $("#u-adapter").value;
  document.querySelectorAll("#usage-form [data-adapters]").forEach((l) => { l.hidden = !l.dataset.adapters.split(" ").includes(a); });
}

async function saveUsage(ev) {
  ev.preventDefault();
  const body = {
    timeZone: $("#u-tz").value.trim(),
    mail: {
      adapter: $("#u-adapter").value,
      to: $("#u-to").value.trim(),
      from: $("#u-from").value.trim(),
      smtp: { host: $("#u-host").value.trim(), port: $("#u-port").value.trim(), security: $("#u-security").value, username: $("#u-user").value.trim() },
      sendmail: { path: $("#u-path").value.trim() },
      m365: { tenantId: $("#u-tenant").value.trim(), clientId: $("#u-client").value.trim(), mailbox: $("#u-mailbox").value.trim() },
    },
  };
  try { fillUsageForm(await api("/api/v1/usage/settings", { method: "PUT", body })); say("Usage settings saved.", "ok"); }
  catch (e) { say(e.message, "error"); }
}

function wireUsage() {
  $("#usage-form").addEventListener("submit", saveUsage);
  $("#u-adapter").addEventListener("change", showAdapterFields);
  $("#u-test").addEventListener("click", async () => {
    try { const r = await api("/api/v1/usage/settings/test", { method: "POST" }); say(r.ok ? `Test sent: ${r.detail}` : `Test not sent: ${r.detail}`, r.ok ? "ok" : "error"); }
    catch (e) { say(e.message, "error"); }
  });
  $("#u-run").addEventListener("click", async () => {
    try { const r = await api("/api/v1/usage/run", { method: "POST" }); say(r.skipped || `Counted${r.made.length ? "; made " + r.made.join(", ") : ""}${r.sent.length ? "; sent " + r.sent.length : ""}.`, "ok"); showUsage(); }
    catch (e) { say(e.message, "error"); }
  });
}

// ------------------------------------------------------------------ tabs and start

const SHOW = { licence: showLicence, usage: showUsage };

function openTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tab").forEach((s) => { s.hidden = s.id !== "tab-" + name; });
  SHOW[name]();
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
      state.me = await api("/api/v1/me");
      if (!state.me.roles.includes("admin")) { say("Administration is for administrators.", "error"); return; }
      $("#who").textContent = (state.me.person && state.me.person.name) || state.me.name || state.me.subject;
      const so = $("#signout");
      so.hidden = false;
      so.addEventListener("click", () => { store.del("vm.token"); store.del("vm.expires"); location.assign("/admin/"); });
    } else {
      $("#who").textContent = "Sign-in is off (development)";
    }
    document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => openTab(b.dataset.tab)));
    $("#licence-form").addEventListener("submit", loadLicence);
    wireUsage();
    licenceBanner();
    openTab(location.hash === "#usage" ? "usage" : "licence");
  } catch (e) {
    say(e.message, "error");
  }
}

start();
