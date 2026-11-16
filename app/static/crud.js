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
      const vals = {}; cfg.fields.forEach(f => { const el = cform.elements[f.k]; vals[f.k] = f.type === "check" ? el.checked : f.type === "number" ? (el.value === "" ? null : +el.value) : el.value; });
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
const parseOdbCsv = text => {
  const lines = text.split(/\r?\n/).filter(line => line.trim());
  if (!lines.length) return [];
  const split = line => line.split(/,(?=(?:[^"]*"[^"]*")*[^"]*$)/).map(v => v.trim().replace(/^"|"$/g, ""));
  const headers = split(lines[0]).map(v => v.toLowerCase().replace(/[\s_-]+/g, ""));
  const index = (...names) => names.map(n => headers.indexOf(n)).find(i => i >= 0);
  const name = index("name", "odb", "odbname"), zone = index("zone", "zonename");
  const lat = index("latitude", "lat"), lon = index("longitude", "lon", "lng");
  if (name == null) return [];
  return lines.slice(1).map(line => {
    const v = split(line), number = i => i == null || v[i] === "" ? null : Number(v[i]);
    return {name: v[name], zone: zone == null ? null : v[zone], latitude: number(lat), longitude: number(lon)};
  }).filter(r => r.name && (r.latitude == null || Number.isFinite(r.latitude)) &&
    (r.longitude == null || Number.isFinite(r.longitude)));
};

const CONFIGS = {
  zones: () => {
    mountCrud($("#crud-a"), {
      title: "Zones", desc: "Group ONUs by city, village, or area. This is for organization only and does not affect ONU configuration.", addLabel: "Add Zone",
      api: "/api/zones", url: () => "/api/zones", cols: [["Name", r => esc(r.name)], ["ONUs", r => r.onus]],
      fields: [{k: "name", label: "Zone name"}], payload: v => ({name: v.name}),
      buttons: [["Delete unused zones", async load => { if (confirm("Delete all zones that are not used by any ONUs?")) { const r = await api("/api/zones/delete-unused", {method: "POST"}); toast(`${r.deleted} zones deleted`); load(); } }],
                ["Import CSV", async load => { const f = await pickFile(); if (!f) return; const r = await api("/api/zones/import", {method: "POST", body: {names: await readNames(f)}}); toast(`${r.created} new zones`); load(); }]],
      exports: [["Export", "/api/zones/export"]]});
  },
  odbs: () => {
    mountCrud($("#crud-a"), {
      title: "ODB (Splitter)", desc: "ODBs/splitters available for assignment to ONUs.", addLabel: "Add ODB",
      api: "/api/odbs", url: () => "/api/odbs",
      cols: [["Name", r => esc(r.name)], ["Zone", r => esc(r.zone) || "—"],
        ["Latitude", r => r.latitude ?? "—"], ["Longitude", r => r.longitude ?? "—"], ["ONUs", r => r.onus]],
      fields: [{k: "name", label: "ODB name"},
        {k: "zone_id", label: "Zone", type: "select", options: async () => [["", "—"], ...(await api("/api/zones")).map(z => [z.id, z.name])]},
        {k: "latitude", label: "Latitude", type: "number", step: "any", min: -90},
        {k: "longitude", label: "Longitude", type: "number", step: "any", min: -180}],
      payload: v => ({name: v.name, zone_id: v.zone_id ? +v.zone_id : null, latitude: v.latitude, longitude: v.longitude}),
      buttons: [["Import CSV", async load => {
        const f = await pickFile(); if (!f) return;
        const rows = parseOdbCsv(await f.text());
        if (!rows.length) throw new Error("CSV tidak memiliki baris ODB yang valid.");
        const r = await api("/api/odbs/import", {method: "POST", body: {rows}});
        toast(`${r.created} ODB dibuat, ${r.updated} diperbarui, ${r.zones_created} zona dibuat`);
        load();
      }], ...(window.USER_ROLE === "administrator" ? [["Import dari GECoverage", async load => {
        const v = await askForm({title: "Import titik ODP dari GECoverage",
          note: "Password hanya dikirim saat import dan tidak disimpan di ControlOLT.",
          fields: [{k: "base_url", label: "URL GECoverage", value: "http://103.76.107.11:2424"},
            {k: "username", label: "Username"}, {k: "password", label: "Password", type: "password"}],
          submit: "Import"});
        if (!v) return;
        const r = await api("/api/odbs/import-gecoverage", {method: "POST", body: v});
        toast(`${r.created} ODB dibuat, ${r.updated} diperbarui, ${r.zones_created} zona dibuat dari ${r.source_rows} titik`);
        load();
      }]] : [])],
      exports: [["Export", "/api/odbs/export"]]});
  },
  "speed-profiles": () => mountCrud($("#crud-a"), {
    title: "Speed profiles", desc: "Download/upload speed limits that can be assigned to ONUs. New profiles are automatically distributed to all OLTs, including those not write-enabled. Existing profile names are not overwritten; results for each OLT are recorded in the Audit log.", addLabel: "Add speed profile",
    tabs: [["download", "Download"], ["upload", "Upload"]], api: "/api/speed-profiles", url: st => `/api/speed-profiles?direction=${st.f}`,
    buttons: window.USER_ROLE === "administrator" ? [
      ["Convert Type 1 fixed T-CONTs to Type 4 Best Effort", async () => {
        const olts = await api("/api/olts");
        if (!olts.length) throw new Error("No OLTs are registered.");
        const results = [];
        for (let i = 0; i < olts.length; i += 3) {
          const batch = await Promise.all(olts.slice(i, i + 3).map(async olt => {
            try {
              const result = await api(`/api/olts/${olt.id}/tcont-profiles`);
              const profiles = result.profiles.filter(profile => profile.type === 1);
              const plan = profiles.length ? await api("/api/plan", {
                method: "POST", body: {action: "convert_tcont", olt_id: olt.id, params: {profiles}},
              }) : null;
              return {olt, profiles, plan, error: ""};
            } catch (error) {
              return {olt, profiles: [], plan: null, error: error.message};
            }
          }));
          results.push(...batch);
        }
        const failed = results.some(result => result.error);
        const candidates = results.filter(result => result.profiles.length);
        const profileCount = candidates.reduce((sum, result) => sum + result.profiles.length, 0);
        const detailRows = results.map(result => {
          const state = result.error ? `Inspection failed: ${result.error}` :
            result.profiles.length ? `${result.profiles.length} profile(s) to convert` : "No Type 1 profiles";
          const commands = result.plan ? `<p class="muted small">${result.plan.warnings.map(esc).join(" ")}</p><details><summary>Review commands and rollback</summary><pre>${esc(result.plan.commands.join("\n"))}\n\nRollback:\n${esc(result.plan.rollback.join("\n"))}</pre></details>` : "";
          return `<details><summary>${esc(result.olt.name)} — ${esc(state)}</summary>${commands}</details>`;
        }).join("");
        const dialog = $("#plan-dialog");
        const applyButton = failed || !candidates.length
          ? "" : '<button type="button" class="primary" id="tcont-apply">Apply all conversions</button>';
        dialog.innerHTML = `<div class="stack"><h3>Type 1 → Type 4 T-CONT preview</h3>
          <p>${failed ? "At least one OLT could not be inspected. No OLT will be changed until every registered OLT is readable." :
            `${profileCount} profile(s) across ${candidates.length} OLT(s) will be converted. Each fixed kbps value will be preserved as the Type 4 maximum.`}</p>
          <div class="scroll">${detailRows}</div><p id="tcont-progress" class="muted small"></p>
          <div class="row end"><button type="button" id="tcont-close">Close</button>${applyButton}</div></div>`;
        $("#tcont-close", dialog).onclick = () => dialog.close();
        if (!failed && candidates.length) {
          $("#tcont-apply", dialog).onclick = async event => {
            const button = event.currentTarget;
            if (!confirm(`Apply Type 1 fixed → Type 4 Best Effort to ${profileCount} profile(s) on ${candidates.length} OLT(s)? Existing kbps values will be preserved.`)) return;
            button.disabled = true;
            const progress = $("#tcont-progress", dialog);
            for (const result of candidates) {
              progress.textContent = `Applying to ${result.olt.name}…`;
              try {
                const applied = await api("/api/exec", {method: "POST", body: {
                  action: "convert_tcont", olt_id: result.olt.id, params: {profiles: result.profiles},
                  confirm: true, save_after: true,
                }});
                if (!applied.ok) throw new Error(applied.message || "OLT rejected the conversion.");
                if (applied.saved && !applied.saved.ok) throw new Error(`Profiles changed, but saving failed: ${applied.saved.message}`);
                result.outcome = "Completed and saved";
              } catch (error) {
                result.outcome = `Failed: ${error.message}`;
                break;
              }
            }
            const complete = candidates.every(result => result.outcome === "Completed and saved");
            dialog.innerHTML = `<div class="stack"><h3>${complete ? "T-CONT conversion complete" : "T-CONT conversion stopped"}</h3>
              <p>${complete ? "All converted profiles were saved." : "Check the Audit log. Completed OLTs remain changed; rerun the preview to continue with profiles still at Type 1."}</p>
              <ul>${candidates.map(result => `<li>${esc(result.olt.name)}: ${esc(result.outcome || "Not attempted")}</li>`).join("")}</ul>
              <div class="row end"><button type="button" id="tcont-close">Close</button></div></div>`;
            $("#tcont-close", dialog).onclick = () => dialog.close();
          };
        }
        dialog.showModal();
      }],
      ["Inspect OLT T-CONT profiles", async () => {
      const olts = await api("/api/olts");
      if (!olts.length) throw new Error("No OLTs are registered.");
      const selected = await askForm({
        title: "Inspect OLT T-CONT profiles",
        note: "Read-only inspection. No OLT configuration will be changed.",
        fields: [{k: "olt_id", label: "Target OLT", type: "select",
          options: olts.map(olt => [olt.id, `${olt.name} (${olt.model})`])}],
        submit: "Inspect",
      });
      if (!selected) return;
      const dialog = $("#plan-dialog");
      if (dialog.open) dialog.close();
      const result = await api(`/api/olts/${selected.olt_id}/tcont-profiles`);
      const rows = result.profiles.map(profile => {
        const parameters = Object.entries(profile.parameters)
          .map(([key, value]) => `${key} ${value} kbps`).join(", ") || "—";
        const kind = profile.type === 1 ? "Type 1 — Fixed" :
          profile.type === 4 ? "Type 4 — Best Effort" : `Type ${profile.type}`;
        return `<tr><td>${esc(profile.name)}</td><td>${esc(kind)}</td><td>${esc(parameters)}</td></tr>`;
      }).join("");
      dialog.innerHTML = `<div class="stack"><h3>T-CONT profiles — ${esc(result.olt)} (${esc(result.model)})</h3>
        <p class="muted small">Read-only result. ${result.type1_count} Type 1 fixed profile(s) found.</p>
        <div class="scroll"><table><thead><tr><th>Profile</th><th>Type</th><th>Parameters</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="3" class="muted">No T-CONT profile definitions were found.</td></tr>'}</tbody></table></div>
        <div class="row end"><button type="button" id="tcont-close">Close</button></div></div>`;
      $("#tcont-close", dialog).onclick = () => dialog.close();
      dialog.showModal();
    }]] : [],
    cols: [["Name", r => esc(r.name)], ["For", () => "Any"], ["Speed", r => `${r.kbps} kbps`], ["Type", r => esc(r.kind)],
           ["Default", r => `<input type="checkbox" disabled ${r.is_default ? "checked" : ""}>`],
           ["ONUs", r => r.onus ? `<a href="/configured?profile_id=${r.id}" title="View ONUs using this profile">${r.onus}</a>` : "0"]],
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
