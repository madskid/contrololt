// Generic CRUD UI for Zones/ODB, Speed profiles, and ONU types.
const dlg = $("#crud-dialog"), cform = $("#crud-form");

function mountCrud(root, cfg) {
  const st = {rows: [], q: "", f: cfg.tabs ? cfg.tabs[0][0] : ""};
  root.innerHTML = `<div class="row between"><div><h2>${esc(cfg.title)}</h2><div class="muted small">${esc(cfg.desc)}</div></div><div class="row gap wrap actions"></div></div>
    ${cfg.tabs ? `<div class="tabs">${cfg.tabs.map(([k, l], i) => `<button data-t="${k}" class="${i ? "" : "active"}">${l}</button>`).join("")}</div>` : ""}
    <div class="card pad row wrap gap"><input class="grow q" placeholder="Search by name">${cfg.filter2 ? cfg.filter2.html : ""}</div>
    <div class="card scroll"><table><thead><tr>${cfg.cols.map(c => `<th>${c[0]}</th>`).join("")}<th>Action</th></tr></thead><tbody></tbody></table></div>`;
  const act = $(".actions", root), tb = $("tbody", root);
  const addBtn = (label, fn, cls = "") => { const b = document.createElement("button"); b.textContent = label; b.className = cls; b.onclick = fn; act.appendChild(b); return b; };
  addBtn("+ " + cfg.addLabel, () => openForm(null), "primary");
  (cfg.buttons || []).forEach(([l, fn]) => addBtn(l, async () => { try { await fn(load); } catch (e) { toast(e.message, true); } }));
  (cfg.exports || []).forEach(([l, href]) => { const a = document.createElement("a"); a.className = "btn"; a.textContent = l; a.href = href; act.appendChild(a); });

  async function load() { st.rows = await api(cfg.url(st)); render(); }
  function render() {
    const q = st.q.toLowerCase(), list = st.rows.filter(r => (!q || String(r.name).toLowerCase().includes(q)) && (!cfg.keep || cfg.keep(r, st, root)));
    tb.innerHTML = list.map(r => `<tr data-id="${r.id}">${cfg.cols.map(c => `<td>${c[1](r)}</td>`).join("")}
      <td class="row">${(cfg.rowButtons || []).map(([l], i) => `<button class="sm" data-r="${i}">${l}</button>`).join("")}<button class="sm" data-a="edit">Edit</button><button class="sm danger" data-a="del">Delete</button></td></tr>`).join("")
      || `<tr><td colspan="${cfg.cols.length + 1}" class="muted">No data available.</td></tr>`;
  }
  async function openForm(row) {
    const opts = {};
    for (const f of cfg.fields) if (f.options) opts[f.k] = await f.options(st);
    const val = k => row ? (cfg.toForm ? cfg.toForm(row)[k] : row[k]) : (typeof cfg.defaults === "function" ? cfg.defaults(st)[k] : (cfg.defaults || {})[k]);
    cform.innerHTML = `<h3>${row ? "Edit" : cfg.addLabel}</h3><div class="grid2">${cfg.fields.map(f => {
      const v = val(f.k);
      if (f.type === "select") return `<label>${f.label}<select name="${f.k}">${opts[f.k].map(([ov, ol]) => `<option value="${esc(ov)}" ${String(ov) === String(v ?? "") ? "selected" : ""}>${esc(ol)}</option>`).join("")}</select></label>`;
      if (f.type === "check") return `<label>${f.label}<input name="${f.k}" type="checkbox" ${v ? "checked" : ""}></label>`;
      return `<label>${f.label}<input name="${f.k}" type="${f.type || "text"}" value="${esc(v ?? "")}" ${f.req === false ? "" : "required"} ${f.step ? `step="${f.step}"` : ""} ${f.min != null ? `min="${f.min}"` : ""}></label>`;
    }).join("")}</div><div class="row end"><button type="button" id="c-cancel">Cancel</button><button class="primary" id="c-save">Save</button></div>`;
    $("#c-cancel").onclick = () => dlg.close();
    cform.onsubmit = async e => {
      e.preventDefault();
      const vals = {}; cfg.fields.forEach(f => { const el = cform.elements[f.k]; vals[f.k] = f.type === "check" ? el.checked : f.type === "number" ? +el.value : el.value; });
      try {
        const saved = await api(row ? `${cfg.api}/${row.id}` : cfg.api, {method: row ? "PUT" : "POST", body: cfg.payload(vals, st)});
        dlg.close();
        toast(Number.isInteger(saved?.push_targets)
          ? saved.push_targets
            ? `Saved. Profile push scheduled to ${saved.push_targets} OLT(s); see the Audit log for results.`
            : "Saved. No OLTs are registered to receive the profile."
          : "Saved");
        load();
      }
      catch (err) { toast(err.message, true); }
    };
    dlg.showModal();
  }
  tb.onclick = async e => {
    const rb = e.target.closest("button[data-r]");
    if (rb) { const row = st.rows.find(r => r.id === +rb.closest("tr").dataset.id); return cfg.rowButtons[+rb.dataset.r][1](row, load); }
    const b = e.target.closest("button[data-a]"); if (!b) return;
    const row = st.rows.find(r => r.id === +b.closest("tr").dataset.id);
    if (b.dataset.a === "edit") return openForm(row);
    if (!confirm(`Delete "${row.name}"?${row.onus ? `\n${row.onus} ONUs use this entry; their references will be cleared.` : ""}`)) return;
    try { await api(`${cfg.api}/${row.id}`, {method: "DELETE"}); load(); } catch (err) { toast(err.message, true); }
  };
  $(".q", root).oninput = ev => { st.q = ev.target.value; render(); };
  $$(".tabs button", root).forEach(b => b.onclick = () => { st.f = b.dataset.t; $$(".tabs button", root).forEach(x => x.classList.toggle("active", x === b)); load(); });
  if (cfg.filter2) { cfg.filter2.bind(root, st, render); }
  load().catch(e => toast(e.message, true));
  return {load};
}

const readNames = file => new Promise(res => { const r = new FileReader(); r.onload = () => res(r.result.split(/\r?\n/).map(l => l.split(",")[0].replace(/^"|"$/g, "").trim()).filter(x => x && x.toLowerCase() !== "name")); r.readAsText(file); });
const pickFile = () => new Promise(res => { const i = document.createElement("input"); i.type = "file"; i.accept = ".csv,.txt"; i.onchange = () => res(i.files[0]); i.click(); });

const CONFIGS = {
  zones: () => {
    mountCrud($("#crud-a"), {
      title: "Zones", desc: "Group ONUs by city, village, or area. This is for organization only and does not affect ONU configuration.", addLabel: "Add Zone",
      api: "/api/zones", url: () => "/api/zones", cols: [["Name", r => esc(r.name)], ["ONUs", r => r.onus]],
      fields: [{k: "name", label: "Zone name"}], payload: v => ({name: v.name}),
      buttons: [["Delete unused zones", async load => { if (confirm("Delete all zones that are not used by any ONUs?")) { const r = await api("/api/zones/delete-unused", {method: "POST"}); toast(`${r.deleted} zones deleted`); load(); } }],
                ["Import CSV", async load => { const f = await pickFile(); if (!f) return; const r = await api("/api/zones/import", {method: "POST", body: {names: await readNames(f)}}); toast(`${r.created} new zones`); load(); }]],
      exports: [["Export", "/api/zones/export"]]});
    mountCrud($("#crud-b"), {
      title: "ODB (Splitter)", desc: "ODBs/splitters available for assignment to ONUs.", addLabel: "Add ODB",
      api: "/api/odbs", url: () => "/api/odbs", cols: [["Name", r => esc(r.name)], ["Zone", r => esc(r.zone) || "—"], ["ONUs", r => r.onus]],
      fields: [{k: "name", label: "ODB name"}, {k: "zone_id", label: "Zone", type: "select", options: async () => [["", "—"], ...(await api("/api/zones")).map(z => [z.id, z.name])]}],
      payload: v => ({name: v.name, zone_id: v.zone_id ? +v.zone_id : null})});
  },
  "speed-profiles": () => mountCrud($("#crud-a"), {
    title: "Speed profiles", desc: "Download/upload speed limits that can be assigned to ONUs. New profiles are automatically distributed to all OLTs, including those not write-enabled. Existing profile names are not overwritten; results for each OLT are recorded in the Audit log.", addLabel: "Add speed profile",
    tabs: [["download", "Download"], ["upload", "Upload"]], api: "/api/speed-profiles", url: st => `/api/speed-profiles?direction=${st.f}`,
    cols: [["Name", r => esc(r.name)], ["For", () => "Any"], ["Speed", r => `${r.kbps} kbps`], ["Type", r => esc(r.kind)],
           ["Default", r => `<input type="checkbox" disabled ${r.is_default ? "checked" : ""}>`], ["ONUs", r => r.onus]],
    fields: [{k: "name", label: "Name (e.g. DOWN-20M)"}, {k: "direction", label: "Direction", type: "select", options: async () => [["download", "Download"], ["upload", "Upload"]]},
             {k: "mbps", label: "Speed (Mbps)", type: "number", step: "any", min: 0.0625}, {k: "is_default", label: "Default", type: "check"}],
    defaults: st => ({direction: st.f, mbps: 10}), toForm: r => ({...r, mbps: r.kbps / 1024}),
    payload: v => ({name: v.name, direction: v.direction, kbps: Math.round(v.mbps * 1024), is_default: v.is_default}),
    rowButtons: [["Push to OLT", async row => {
      const olts = (await api("/api/filters")).olts;
      const v = await askForm({title: `Create profile ${row.name} on OLT`, fields: [{k: "olt_id", label: "Target OLT", type: "select", options: olts.map(o => [o.id, o.name])}], submit: "Preview commands"});
      if (v) openPlan({action: "push_speed", olt_id: +v.olt_id, params: {profile_id: row.id}});
    }]],
    exports: [["Export", "/api/speed-profiles-export"]]}),
  "onu-types": () => mountCrud($("#crud-a"), {
    title: "ONU types", desc: "ONU model, PON type, and port counts.", addLabel: "Add ONU type",
    api: "/api/onu-types", url: () => "/api/onu-types",
    cols: [["PON type", r => r.pon_type], ["Channels", r => esc(r.channels)], ["ONU type", r => esc(r.name)], ["ONUs", r => r.onus],
           ["Ethernet ports", r => r.eth_ports], ["WiFi", r => r.wifi_ports], ["VoIP ports", r => r.voip_ports], ["CATV", r => r.catv],
           ["Allow custom profiles", r => r.allow_custom ? "Yes" : "No"], ["Capability", r => esc(r.capability)]],
    fields: [{k: "name", label: "ONU type name"}, {k: "pon_type", label: "PON type", type: "select", options: async () => [["GPON", "GPON"], ["EPON", "EPON"]]},
             {k: "channels", label: "Channels (G, G/XG/XGS)"}, {k: "eth_ports", label: "Ethernet ports", type: "number", min: 0},
             {k: "wifi_ports", label: "WiFi ports", type: "number", min: 0}, {k: "voip_ports", label: "VoIP ports", type: "number", min: 0},
             {k: "catv", label: "CATV (0/1)", type: "number", min: 0}, {k: "allow_custom", label: "Allow custom profiles", type: "check"},
             {k: "capability", label: "Capability", type: "select", options: async () => ["Bridging/Routing", "Bridging", "Routing"].map(x => [x, x])}],
    defaults: {pon_type: "GPON", channels: "G", eth_ports: 4, wifi_ports: 0, voip_ports: 0, catv: 0, allow_custom: true, capability: "Bridging/Routing"},
    payload: v => v,
    buttons: [["Import from registered ONUs", async load => { const r = await api("/api/onu-types/import-from-onus", {method: "POST"}); toast(`${r.created} new types`); load(); }]],
    exports: [["Export", "/api/onu-types-export"]]}),
};
CONFIGS[window.CRUD]();
