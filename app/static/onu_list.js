// Configured & Diagnostics pages: full filters, server-side pagination, CSV export, and live updates.
const DIAG = window.LIST_MODE === "diag";
const S = {page: 1, status: new Set(), signal: new Set()};
const STATUS_BTNS = [["online", "Online", "#16a34a"], ["power_fail", "Power fail", "#d97706"], ["los", "LOS", "#dc2626"],
                     ["offline", "Offline", "#8b93a5"], ["unknown", "Unknown", "#7c8aa5"]];
const SIG_BTNS = [["good", "▂▄▆ Good", "#16a34a"], ["warn", "▂▄ Warning", "#d97706"], ["crit", "▂ Critical", "#dc2626"]];
const sel = (id, label) => `<label>${label}<select id="${id}"></select></label>`;
let pages = 1;

$("#filters").innerHTML = `<div class="filters">
  <label class="wide">Search<input id="f-q" placeholder="SN, MAC, name, address, contact, PPPoE, external ID"></label>
  ${sel("f-olt", "OLT")}${sel("f-slot", "Board")}${sel("f-pon", "Port")}${sel("f-zone", "Zone")}${sel("f-odb", DIAG ? "Splitter" : "ODB")}
  ${DIAG ? "" : sel("f-vlan", "VLAN")}${sel("f-type", "ONU type")}${DIAG ? "" : sel("f-profile", "Profile")}
  ${DIAG ? '<label>Changed from<input id="f-from" type="date"></label><label>Changed to<input id="f-to" type="date"></label>' : ""}
</div>
<div class="filters-bar">
  <label>PON type<span class="seg"><button type="button" disabled title="EPON is not supported yet">EPON</button><button type="button" class="on">GPON</button></span></label>
  <label>Status<span class="seg" id="seg-status">${STATUS_BTNS.map(([k, l, c]) => `<button type="button" data-k="${k}" title="${l}"><span class="dotc" style="background:${c}"></span>${l}</button>`).join("")}</span></label>
  <label>Signal<span class="seg" id="seg-signal">${SIG_BTNS.map(([k, l, c]) => `<button type="button" data-k="${k}" style="color:${c}">${l}</button>`).join("")}</span></label>
  <span class="row gap push"><button type="button" id="f-reset">Reset</button><button type="button" id="f-refresh">Refresh</button><a class="btn" id="f-export" href="#">Export CSV</a></span>
</div>`;

const COLS = DIAG ? [
  ["Status", o => badge(o.status), "st"], ["Rx OLT", o => rxbar(opticalValue(o.status, o.olt_rx_power), o.rx_warn, o.rx_crit), "rxl"],
  ["Rx ONU", o => rxbar(opticalValue(o.status, o.rx_power), o.rx_warn, o.rx_crit), "rxo"], ["Distance", o => o.distance_m == null ? "—" : o.distance_m + " m"],
  ["Name", o => esc(o.name) || "—"], ["SN / MAC", o => `<a href="/onus/${o.id}">${esc(o.sn)}</a>`],
  ["Zone", o => esc(o.zone) || "—"], ["Splitter", o => esc(o.odb) || "—"],
  ["ONU", o => `${esc(o.olt_name)}<div class="muted small">${esc(o.location)}</div>`], ["Changed", o => fmtChanged(o.status_changed_at)],
] : [
  ["Status", o => badge(o.status), "st"], ["View", o => `<a class="btn" href="/onus/${o.id}">View</a>`],
  ["Name", o => esc(o.name) || "—"], ["SN / MAC", o => esc(o.sn)],
  ["ONU", o => `${esc(o.olt_name)}<div class="muted small">${esc(o.location)}</div>`],
  ["Zone", o => esc(o.zone) || "—"], ["ODB", o => esc(o.odb) || "—"],
  ["Signal", o => rxbar(opticalValue(o.status, o.rx_power), o.rx_warn, o.rx_crit), "rxo"],
  ["B/R", o => o.mode === "bridge" ? "Bridge" : (o.mode === "routing" || o.conn_type) ? "R" + (o.conn_type ? ": " + esc(o.conn_type.toUpperCase()) : "") : "—"],
  ["VLAN", o => o.vlan ?? "—"], ["Type", o => esc(o.onu_type)], ["Auth date", o => fmtDate(o.auth_date)],
];
$("#onu-table thead").innerHTML = "<tr>" + COLS.map(c => `<th>${c[0]}</th>`).join("") + "</tr>";

function params(extra = {}) {
  const p = new URLSearchParams();
  [["olt_id", "f-olt"], ["slot", "f-slot"], ["pon", "f-pon"], ["zone_id", "f-zone"], ["odb_id", "f-odb"], ["vlan", "f-vlan"],
   ["onu_type", "f-type"], ["profile_id", "f-profile"], ["q", "f-q"], ["changed_from", "f-from"], ["changed_to", "f-to"]]
    .forEach(([k, id]) => { const el = $("#" + id); if (el && el.value) p.set(k, el.value); });
  if (S.status.size) p.set("status", [...S.status].join(","));
  if (S.signal.size) p.set("signal", [...S.signal].join(","));
  if (DIAG) p.set("sort", "changed");
  Object.entries(extra).forEach(([k, v]) => p.set(k, v));
  return p;
}
function row(o) {
  return `<tr data-id="${o.id}" data-warn="${o.rx_warn}" data-crit="${o.rx_crit}">` +
    COLS.map(c => `<td${c[2] ? ` class="${c[2]}"` : ""}>${c[1](o)}</td>`).join("") + "</tr>";
}
async function load() {
  const r = await api("/api/onus?" + params({page: S.page, per_page: 50}));
  $("#onu-table tbody").innerHTML = r.items.map(row).join("") || `<tr><td colspan="${COLS.length}" class="muted">No matching ONUs found.</td></tr>`;
  tiles($("#tiles"), r.counts);
  pages = Math.max(1, Math.ceil(r.total / r.per_page));
  const a = r.total ? (r.page - 1) * r.per_page + 1 : 0, b = Math.min(r.total, r.page * r.per_page);
  $("#count").textContent = `${a}-${b} ONUs of ${r.total} displayed`;
  $$(".pageinfo").forEach(e => e.textContent = `${S.page} / ${pages}`);
  $$(".prev").forEach(e => e.disabled = S.page <= 1); $$(".next").forEach(e => e.disabled = S.page >= pages);
  $("#f-export").href = "/api/onus-export?" + params();
}
const reload = () => { S.page = 1; load().catch(e => toast(e.message, true)); };

async function loadFilters(oltId) {
  const f = await api("/api/filters" + (oltId ? `?olt_id=${oltId}` : ""));
  window.FILTERS = f;
  if (!oltId) fillSelect($("#f-olt"), f.olts, "id", "name");
  fillSelect($("#f-slot"), f.boards.map(b => ({v: b, l: b})), "v", "l");
  fillPorts();
  fillSelect($("#f-zone"), f.zones, "id", "name"); fillSelect($("#f-odb"), f.odbs, "id", "name");
  if ($("#f-vlan")) fillSelect($("#f-vlan"), f.vlans.map(v => ({v, l: v})), "v", "l");
  fillSelect($("#f-type"), f.onu_types.map(t => ({v: t, l: t})), "v", "l");
  if ($("#f-profile")) fillSelect($("#f-profile"), f.profiles.map(p => ({id: p.id, name: `${p.name} (${p.direction === "download" ? "DL" : "UL"})`})), "id", "name");
}
function fillPorts() {
  const slot = $("#f-slot").value, ports = (window.FILTERS?.ports || []).filter(p => !slot || String(p.slot) === slot);
  fillSelect($("#f-pon"), [...new Set(ports.map(p => p.pon))].map(v => ({v, l: v})), "v", "l");
}
$("#f-olt").onchange = async () => { $("#f-slot").value = ""; $("#f-pon").value = ""; await loadFilters($("#f-olt").value); reload(); };
$("#f-slot").onchange = () => { fillPorts(); reload(); };
["f-pon", "f-zone", "f-odb", "f-vlan", "f-type", "f-profile", "f-from", "f-to"].forEach(id => { const e = $("#" + id); if (e) e.onchange = reload; });
$("#f-q").oninput = debounce(reload);
function toggle(segId, set) {
  $(segId).onclick = e => {
    const b = e.target.closest("button[data-k]"); if (!b) return;
    set.has(b.dataset.k) ? set.delete(b.dataset.k) : set.add(b.dataset.k);
    b.classList.toggle("on", set.has(b.dataset.k)); reload();
  };
}
toggle("#seg-status", S.status); toggle("#seg-signal", S.signal);
$("#f-reset").onclick = () => { $$("#filters input, #filters select").forEach(e => e.value = ""); S.status.clear(); S.signal.clear();
  $$("#filters .seg button").forEach(b => b.classList.remove("on")); $$("#filters .seg button:last-child")[0]; loadFilters().then(reload); };
$("#f-refresh").onclick = () => load().catch(e => toast(e.message, true));
$$(".prev").forEach(b => b.onclick = () => { if (S.page > 1) { S.page--; load(); } });
$$(".next").forEach(b => b.onclick = () => { if (S.page < pages) { S.page++; load(); } });

onEvent(m => {
  if (m.type === "onus") for (const c of m.changes) {
    const tr = document.querySelector(`#onu-table tr[data-id="${c.id}"]`); if (!tr) continue;
    const w = +tr.dataset.warn, k = +tr.dataset.crit;
    tr.querySelector(".st").innerHTML = badge(c.status);
    const o = tr.querySelector(".rxo"); if (o) o.innerHTML = rxbar(opticalValue(c.status, c.rx_power), w, k);
    const l = tr.querySelector(".rxl"); if (l) l.innerHTML = rxbar(opticalValue(c.status, c.olt_rx_power), w, k);
  } else if (m.type === "onus_reload") load().catch(() => {});
});
loadFilters().then(load).catch(e => toast(e.message, true));
