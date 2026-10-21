let OLTS = new Map(), META = null, editingId = null;
const dlg = $("#olt-dialog"), form = $("#olt-form");

function row(o) {
  const save = o.last_save_at ? ago(o.last_save_at) + (o.last_save_ok === false ? " ⚠" : "") : (o.last_save_ok === false ? "failed ⚠" : "never");
  return `<tr data-id="${o.id}"><td>${o.id}</td><td>${badge(o.status)}</td>
  <td><a href="/olts/${o.id}"><b>${esc(o.name)}</b></a><div class="muted small" title="${esc(o.status_msg)}">${esc((o.status_msg || "").slice(0, 70))}</div></td>
  <td>${esc(o.ip)}</td><td>${o.telnet_port}</td><td>${o.snmp_port}</td><td>${esc(o.hw_version)}</td><td>${esc(o.sw_version) || "—"}</td>
  <td class="num">${o.counts.total}</td><td>${o.write_enabled ? '<span class="badge st-online">ON</span>' : '<span class="muted">off</span>'}</td><td>${ago(o.last_poll_at)}${o.poll_source ? ` <span class="muted small">(${esc(o.poll_source)})</span>` : ""}</td>
  <td title="${esc(o.last_save_msg)}">${save}</td>
  <td class="row"><a class="btn" href="/olts/${o.id}">View</a><button class="sm" data-act="sync">Sync</button><button class="sm" data-act="save">Save</button>
  <button class="sm" data-act="edit">Edit</button>  <button class="sm danger" data-act="del">Delete</button></td></tr>`;
}
function render() {
  const q = ($("#f-q").value || "").toLowerCase(), st = $("#f-status").value;
  const list = [...OLTS.values()].filter(o => (!st || o.status === st) &&
    (!q || [o.name, o.ip, o.model, o.hw_version].some(x => (x || "").toLowerCase().includes(q))));
  $("#olt-table tbody").innerHTML = list.map(row).join("");
  $("#olt-empty").hidden = OLTS.size > 0;
}
async function load() {
  const [list, meta] = await Promise.all([api("/api/olts"), META || api("/api/meta")]);
  META = meta; OLTS = new Map(list.map(o => [o.id, o])); render(); fillModels();
}
function fillModels() {
  const sel = form.elements.model_sel; if (sel.options.length) return;
  META.models.forEach(m => sel.add(new Option(m.label, m.key)));
  sel.add(new Option("Other (enter manually)…", "__manual__"));
  Object.entries(META.families).forEach(([k, v]) => form.elements.cli_family.add(new Option(v, k)));
  sel.onchange = syncModelUI; syncModelUI();
}
function syncModelUI() {
  const manual = form.elements.model_sel.value === "__manual__";
  $("#model-manual-wrap").hidden = !manual; $("#family-wrap").hidden = !manual;
}
function payload() {
  const f = form.elements, manual = f.model_sel.value === "__manual__";
  const p = {name: f.name.value, ip: f.ip.value, model: manual ? f.model_manual.value : f.model_sel.value,
    shelf: +f.shelf.value, telnet_port: +f.telnet_port.value, telnet_user: f.telnet_user.value,
    telnet_pass: f.telnet_pass.value, enable_pass: f.enable_pass.value,
    snmp_community: f.snmp_community.value, snmp_port: +f.snmp_port.value, write_enabled: f.write_enabled.checked};
  if (manual) p.cli_family = f.cli_family.value;
  return p;
}
function openDialog(o) {
  editingId = o ? o.id : null; form.reset(); $("#test-out").hidden = true;
  $("#dlg-title").textContent = o ? "Edit OLT" : "Add OLT"; $("#pw-hint").hidden = !o;
  form.elements.telnet_pass.required = !o;
  if (o) {
    form.elements.name.value = o.name; form.elements.ip.value = o.ip; form.elements.shelf.value = o.shelf;
    form.elements.telnet_port.value = o.telnet_port; form.elements.telnet_user.value = o.telnet_user; form.elements.snmp_port.value = o.snmp_port;
    const known = META.models.some(m => m.key === o.model);
    form.elements.model_sel.value = known ? o.model : "__manual__";
    if (!known) { form.elements.model_manual.value = o.model; form.elements.cli_family.value = o.cli_family; }
    form.elements.snmp_community.value = ""; form.elements.write_enabled.checked = !!o.write_enabled;
  }
  syncModelUI(); dlg.showModal();
}
$("#btn-add").onclick = () => openDialog(null);
$("#btn-cancel").onclick = () => dlg.close();
$("#btn-test").onclick = async () => {
  const out = $("#test-out"); out.hidden = false; out.textContent = "Testing SNMP & Telnet…";
  try {
    const p = payload();     if (editingId && !p.telnet_pass) throw new Error("Enter the Telnet password to test the connection");
    const r = await api("/api/olts/test", {method: "POST", body: p});
    out.textContent = `SNMP  : ${r.snmp_ok ? "OK" : "FAILED"} — ${r.snmp_msg}\nTelnet: ${r.telnet_ok ? "OK" : "FAILED"} — ${r.telnet_msg}` +
      (r.sys_descr ? `\nsysDescr: ${r.sys_descr}` : "");
  } catch (e) { out.textContent = "Error: " + e.message; }
};
form.onsubmit = async ev => {
  ev.preventDefault();
  const btn = $("#btn-save"); btn.disabled = true;
  try {
    const p = payload();
    if (editingId) { ["telnet_pass", "enable_pass", "snmp_community"].forEach(k => { if (!p[k]) delete p[k]; }); }
    const o = await api(editingId ? `/api/olts/${editingId}` : "/api/olts", {method: editingId ? "PUT" : "POST", body: p});
    OLTS.set(o.id, o); render(); dlg.close();
    toast(editingId ? "OLT updated; scheduling a resync…" : "OLT added — retrieving SNMP, running-config, and all ONUs…");
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
};
$("#olt-table").addEventListener("click", async ev => {
  const b = ev.target.closest("button[data-act]"); if (!b) return;
  const id = +b.closest("tr").dataset.id, o = OLTS.get(id);
  try {
    if (b.dataset.act === "edit") openDialog(o);
    else if (b.dataset.act === "sync") { await api(`/api/olts/${id}/sync`, {method: "POST"}); toast("Sync scheduled"); }
    else if (b.dataset.act === "save") {
      b.disabled = true; const r = await api(`/api/olts/${id}/save-config`, {method: "POST"});
      toast(r.ok ? `Config saved on ${o.name}` : `Save failed: ${r.message}`, !r.ok); b.disabled = false;
    } else if (b.dataset.act === "del" && confirm(`Delete OLT "${o.name}" and all its ONU data from the dashboard?\n(The physical OLT will not be changed.)`)) {
      await api(`/api/olts/${id}`, {method: "DELETE"}); OLTS.delete(id); render();
    }
  } catch (e) { toast(e.message, true); b.disabled = false; }
});
onEvent(m => {
  if (m.type === "olt") { const o = OLTS.get(m.id); if (!o) return load(); Object.assign(o, m); render(); }
  else if (m.type === "counts") { const o = OLTS.get(m.olt_id); if (o) { o.counts = m.counts; render(); } }
  else if (m.type === "olt_deleted") { OLTS.delete(m.id); render(); }
});
load().catch(e => toast(e.message, true));
setInterval(() => OLTS.size && render(), 30000); // Refresh the "x minutes ago" label.

$("#f-q").oninput = render; $("#f-status").onchange = render;
