const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const LABEL = {online:"Online", offline:"Offline", los:"LOS", power_fail:"Power fail", unknown:"Unknown", pending:"Pending", syncing:"Syncing"};
const opticalValue = (status, value) => status === "los" || status === "power_fail" ? null : value;

async function api(path, opts = {}) {
  const o = {credentials: "same-origin", headers: {}, ...opts};
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  if (r.status === 401) { location.href = "/login"; throw new Error("Not logged in"); }
  let data = null; try { data = await r.json(); } catch {}
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map(e => (e.loc || []).slice(1).join(".") + ": " + e.msg).join("; ") : r.statusText);
  }
  return data;
}
function toast(msg, bad) {
  const d = document.createElement("div"); d.textContent = msg; if (bad) d.className = "bad";
  $("#toast").appendChild(d); setTimeout(() => d.remove(), bad ? 7000 : 3500);
}
const badge = st => `<span class="badge st-${esc(st)}">${esc(LABEL[st] || st)}</span>`;
const rxClass = (v, warn = -30, crit = -32) => v >= warn ? "rx-ok" : v >= crit ? "rx-warn" : "rx-bad";
function rx(v, warn, crit) {
  if (v === null || v === undefined) return '<span class="muted">—</span>';
  return `<span class="${rxClass(v, warn, crit)}">${v.toFixed(2)}</span>`;
}
function rxbar(v, warn, crit) {
  if (v === null || v === undefined) return '<span class="muted">—</span>';
  const c = rxClass(v, warn, crit), w = Math.max(3, Math.min(100, (v + 40) / 32 * 100));
  return `<span class="bar ${c}"><i style="width:${w}%"></i></span><span class="${c}">${v.toFixed(2)}</span>`;
}
function ago(iso) {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return Math.round(s) + " sec ago"; if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " hr ago"; return new Date(iso).toLocaleString("en-US");
}
const fmtDate = iso => iso ? new Date(iso).toLocaleDateString("en-US", {day: "2-digit", month: "short", year: "numeric"}) : "—";
function fmtChanged(iso) {
  if (!iso) return '<span class="muted">—</span>';
  const d = new Date(iso), now = new Date(), t = d.toLocaleTimeString("en-US", {hour12: false});
  const day = d.toDateString() === now.toDateString() ? "Today" : d.toLocaleDateString("en-US");
  return `${day} ${t}<div class="muted small">${ago(iso)}</div>`;
}
function tiles(el, c) {
  el.innerHTML = [["Total ONU", c.total, ""], ["Online", c.online, "ok"], ["Offline", c.offline, ""],
    ["LOS", c.los, "bad"], ["Power fail", c.power_fail, "warn"], ["Unknown", c.unknown, ""]]
    .map(([l, v, k]) => `<div class="tile ${k}"><b>${v ?? 0}</b><span>${l}</span></div>`).join("");
}
function debounce(fn, ms = 300) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function fillSelect(el, items, vk, lk, any = "Any") {
  const cur = el.value;
  el.innerHTML = `<option value="">${esc(any)}</option>` + items.map(x => `<option value="${esc(x[vk])}">${esc(x[lk])}</option>`).join("");
  if ([...el.options].some(o => o.value === cur)) el.value = cur;
}

// ── WebSocket realtime: events are sent to the page through the "olt-event" window event.
let wsRetry = 1000;
function connectWS() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { wsRetry = 1000; $("#ws-dot").classList.remove("off"); $("#ws-text").textContent = "realtime"; };
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.type !== "ping" && m.type !== "hello") window.dispatchEvent(new CustomEvent("olt-event", {detail: m})); };
  ws.onclose = () => { $("#ws-dot").classList.add("off"); $("#ws-text").textContent = "disconnected…"; setTimeout(connectWS, wsRetry); wsRetry = Math.min(wsRetry * 2, 15000); };
}
if ($("#ws-dot")) connectWS();
function onEvent(fn) { window.addEventListener("olt-event", e => fn(e.detail)); }

// ── "Save config" menu: save config on all OLTs now
const navSave = $("#nav-save");
if (navSave) navSave.onclick = async e => {
  e.preventDefault();
  if (!confirm("Save the configuration on ALL OLTs to OLT memory now?")) return;
  toast("Saving configuration…");
  try {
    const r = await api("/api/olts/save-all", {method: "POST"}), bad = r.results.filter(x => !x.ok);
    toast(bad.length ? `Failed on ${bad.length} OLT(s): ${bad.map(x => x.olt).join(", ")}` : `Config saved on ${r.results.length} OLT(s)`, bad.length > 0);
  } catch (err) { toast(err.message, true); }
};
