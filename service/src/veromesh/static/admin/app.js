/* VeroMesh Administration: the install's licence, its usage reports, notices to owners and the
   MCP gateway's systems. Administrators only.
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

// ------------------------------------------------------------------ notices

function fillNotify(n) {
  $("#n-channels").value = n.channels.join(",");
  $("#n-show").value = n.show;
  $("#n-reminder").value = String(n.reminderBusinessDays);
  $("#n-decisions").value = String(n.decisions);
}

async function showNotices() {
  try { fillNotify(await api("/api/v1/notify/settings")); } catch (e) { say(e.message, "error"); }
}

function wireNotices() {
  $("#notify-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const ch = $("#n-channels").value;
    const body = { channels: ch ? ch.split(",") : [], show: $("#n-show").value, reminderBusinessDays: Number($("#n-reminder").value), decisions: $("#n-decisions").value === "true" };
    try { fillNotify(await api("/api/v1/notify/settings", { method: "PUT", body })); say("Notice settings saved.", "ok"); }
    catch (e) { say(e.message, "error"); }
  });
  $("#n-run").addEventListener("click", async () => {
    try {
      const r = await api("/api/v1/notify/run", { method: "POST" });
      if (r.skipped) { say(r.skipped, "ok"); return; }
      const problems = r.notSent.map((x) => `${x.person}: ${x.problem}`).join("; ");
      say(`Sent ${r.sent.length}.` + (problems ? ` Not sent: ${problems}` : ""), problems ? "error" : "ok");
    } catch (e) { say(e.message, "error"); }
  });
}

// ------------------------------------------------------------------ gateway

const gw = { targets: [], tools: {}, actions: [] };
const ARGS = [["amount", "Amount"], ["currency", "Currency"], ["about", "Person it is about"], ["value", "Value (%, steps)"], ["note", "Note for the log"]];

function option(value, text, selected) {
  const o = el("option", null, text);
  o.value = value;
  if (selected) o.selected = true;
  return o;
}

function argSelect(args, chosen) {
  const sel = el("select");
  sel.append(option("", "–", !chosen));
  const names = args.includes(chosen) || !chosen ? args : [...args, chosen];
  for (const a of names) sel.append(option(a, a, a === chosen));
  return sel;
}

function labelled(text, control) {
  const l = el("label", null, text);
  l.append(control);
  return l;
}

function toolRow(t, tool) {
  const row = el("div", "gw-tool");
  row.dataset.tool = tool.name;
  const name = el("div", "tool");
  name.append(el("div", "mono", tool.name));
  if (tool.description) name.append(el("div", "meta", tool.description));
  if (!tool.listed && gw.tools[t.name] && !gw.tools[t.name].problem) name.append(el("span", "chip bad", "No longer offered by the server"));
  const offered = el("select");
  offered.className = "gw-offered";
  offered.setAttribute("aria-label", `How ${tool.name} is offered`);
  offered.append(option("", "Not offered to agents", !tool.offered), option("read", "Read-only: passes through unchecked", tool.offered === "read"), option("checked", "Checked by VeroMesh first", tool.offered === "checked"));
  row.append(name, offered);
  const m = tool.mapping || {};
  const map = el("div", "gw-map settings");
  const action = el("select");
  action.className = "gw-action";
  action.append(option("", "Choose the action…", !m.action));
  const known = gw.actions.some((a) => a.id === m.action);
  for (const a of gw.actions) action.append(option(a.id, a.name ? `${a.name} (${a.id})` : a.id, a.id === m.action));
  if (m.action && !known) action.append(option(m.action, `${m.action} (not in the model)`, true));
  const wide = labelled("The VeroMesh action it takes", action);
  wide.className = "wide";
  map.append(wide);
  for (const [k, label] of ARGS) {
    const sel = argSelect(tool.arguments, m[k]);
    sel.dataset.arg = k;
    map.append(labelled(`${label}: from the argument`, sel));
    if (k === "currency") {
      const fixed = el("input");
      fixed.type = "text";
      fixed.placeholder = "e.g. USD";
      fixed.dataset.fixed = "currencyValue";
      fixed.value = m.currencyValue || "";
      map.append(labelled("…or always this currency", fixed));
    }
  }
  row.append(map);
  const sync = () => { map.hidden = offered.value !== "checked"; };
  offered.addEventListener("change", sync);
  sync();
  return row;
}

function targetBlock(t) {
  const box = el("div", "gw-target");
  box.dataset.target = t.name;
  const head = el("div", "gw-head");
  head.append(el("span", "name", t.name), el("span", "url", t.url), el("span", "spacer"));
  const remove = el("button", "secondary", "Remove");
  remove.type = "button";
  remove.addEventListener("click", () => { gw.targets = gw.targets.filter((x) => x.name !== t.name); drawGateway(); say(`${t.name} removed; save to apply.`, "ok"); });
  head.append(remove);
  box.append(head);
  box.append(el("div", "meta", t.header ? `Sends ${t.header} from the environment variable ${t.secretEnv}. Agents see its tools as ${t.name}__<tool>.` : `No header is sent. Agents see its tools as ${t.name}__<tool>.`));
  const listed = gw.tools[t.name];
  if (!listed) {
    box.append(el("div", "empty", "Save the settings, then its tools are listed here."));
    return box;
  }
  if (listed.problem) box.append(el("div", "message error", listed.problem));
  if (!listed.tools.length) { box.append(el("div", "empty", "No tools listed.")); return box; }
  for (const tool of listed.tools) box.append(toolRow(t, tool));
  return box;
}

function drawGateway() {
  const box = $("#gw-targets");
  if (!gw.targets.length) { box.replaceChildren(el("div", "empty", "No systems yet: add one below.")); return; }
  box.replaceChildren(...gw.targets.map(targetBlock));
}

function collectGateway() {
  const targets = gw.targets.map((t) => {
    const out = { name: t.name, url: t.url, tools: {}, passThrough: [] };
    if (t.header) Object.assign(out, { header: t.header, secretEnv: t.secretEnv });
    const block = document.querySelector(`.gw-target[data-target="${CSS.escape(t.name)}"]`);
    const rows = block ? block.querySelectorAll("[data-tool]") : [];
    if (!rows.length) return Object.assign(out, { tools: t.tools || {}, passThrough: t.passThrough || [] });
    for (const tr of rows) {
      const offered = tr.querySelector(".gw-offered").value;
      if (offered === "read") out.passThrough.push(tr.dataset.tool);
      if (offered !== "checked") continue;
      const action = tr.querySelector(".gw-action").value;
      if (!action) throw new Error(`${t.name}: choose the VeroMesh action ${tr.dataset.tool} takes.`);
      const m = { action };
      tr.querySelectorAll("[data-arg]").forEach((x) => { if (x.value) m[x.dataset.arg] = x.value; });
      const fixed = tr.querySelector("[data-fixed]").value.trim();
      if (fixed && !m.currency) m.currencyValue = fixed;
      out.tools[tr.dataset.tool] = m;
    }
    return out;
  });
  return { targets };
}

async function loadTools() {
  gw.tools = {};
  await Promise.all(gw.targets.map(async (t) => {
    try { gw.tools[t.name] = await api(`/api/v1/gateway/targets/${encodeURIComponent(t.name)}/tools`); }
    catch (e) { gw.tools[t.name] = { tools: [], problem: e.message }; }
  }));
}

async function showGateway() {
  try {
    const [settings, actions] = await Promise.all([api("/api/v1/gateway/settings"), api("/api/v1/actions").catch(() => [])]);
    gw.targets = settings.targets;
    gw.actions = (actions || []).map((a) => ({ id: a.id, name: a.name })).sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));
    await loadTools();
    drawGateway();
  } catch (e) { say(e.message, "error"); }
}

function wireGateway() {
  $("#gw-add").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const t = { name: $("#gw-name").value.trim(), url: $("#gw-url").value.trim(), tools: {}, passThrough: [] };
    const header = $("#gw-header").value.trim();
    const secret = $("#gw-secret").value.trim();
    if (header || secret) {
      if (!header || !secret) { say("Give both the header and the environment variable, or neither.", "error"); return; }
      Object.assign(t, { header, secretEnv: secret });
    }
    if (gw.targets.some((x) => x.name === t.name)) { say(`There is already a system called ${t.name}.`, "error"); return; }
    gw.targets.push(t);
    ev.target.reset();
    drawGateway();
    say(`${t.name} added; save to list its tools.`, "ok");
  });
  $("#gw-save").addEventListener("click", async () => {
    try {
      const saved = await api("/api/v1/gateway/settings", { method: "PUT", body: collectGateway() });
      gw.targets = saved.targets;
      await loadTools();
      drawGateway();
      say("Gateway settings saved.", "ok");
    } catch (e) { say(e.message, "error"); }
  });
  $("#gw-run").addEventListener("click", async () => {
    try { const r = await api("/api/v1/gateway/run", { method: "POST" }); say(`Sent ${r.released} held call(s).`, "ok"); }
    catch (e) { say(e.message, "error"); }
  });
}

// ------------------------------------------------------------------ tabs and start

const SHOW = { licence: showLicence, usage: showUsage, notices: showNotices, gateway: showGateway };

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
    wireNotices();
    wireGateway();
    licenceBanner();
    openTab(SHOW[location.hash.slice(1)] ? location.hash.slice(1) : "licence");
  } catch (e) {
    say(e.message, "error");
  }
}

start();
