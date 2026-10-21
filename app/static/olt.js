const id = window.OLT_ID, loaded = {};
const kv = rows => `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
const fmtUp = s => s == null ? "—" : (d => `${d}d ${Math.floor(s % 86400 / 3600)}h`)(Math.floor(s / 86400));
const raw = t => `<details><summary>Raw OLT output</summary><pre class="out">${esc(t || "(empty)")}</pre></details>`;
const fail = (el, e) => el.innerHTML = `<p class="err">${esc(e.message)}</p>`;

async function tDetails() {
  const el = $("#tab-details"), o = await api(`/api/olts/${id}`);
  $("#olt-title").textContent = o.name;
  el.innerHTML = kv([["Status", `${badge(o.status)} ${esc(o.status_msg)}`], ["Name", esc(o.name)], ["IP", esc(o.ip)], ["Hardware", esc(o.hw_version)],
    ["Software", esc(o.sw_version) || "—"],     ["Telnet / SNMP port", `${o.telnet_port} / ${o.snmp_port}`], ["CLI style", esc(o.cli_family)],
    ["Uptime (SNMP)", fmtUp(o.uptime_s)], ["sysDescr", esc(o.sys_descr) || "—"], ["Poll source", esc(o.poll_source) || "—"],
    ["Last poll", ago(o.last_poll_at)], ["Last config sync", ago(o.last_full_sync_at)],
    ["Config saved", o.last_save_at ? `${ago(o.last_save_at)}${o.last_save_ok === false ? " (last attempt failed)" : ""}` : "never"], ["Save message", esc(o.last_save_msg) || "—"],
    ["Changes to OLT", o.write_enabled ? "Enabled" : '<span class="err">Disabled</span> (enable in Settings → OLTs → Edit)'], ["ONUs", `${o.counts.total} (online ${o.counts.online}, offline ${o.counts.offline}, LOS ${o.counts.los}, power fail ${o.counts.power_fail})`]]) +
    `<div class="row gap pad"><button id="d-sync">Sync now</button><button id="d-save">Save config to OLT</button><button id="d-diagbtn">Check OLT health</button><a class="btn" href="/configured?olt=${id}">View ONUs</a></div><div id="d-diag"></div>`;
  $("#d-diagbtn").onclick = async ev => {
    ev.target.disabled = true; $("#d-diag").innerHTML = '<p class="muted">Running diagnostic commands…</p>';
    try {
      const r = await api(`/api/olts/${id}/diag`);
      $("#d-diag").innerHTML = `<p>OLT uptime: <b>${fmtUp(r.uptime_s)}</b> <span class="muted small">(temperature/fan/CPU/memory output format is unverified — raw output is shown)</span></p>` +
        r.rows.map(x => `<details ${x.ok ? "" : ""}><summary>${esc(x.command)} ${x.ok ? "" : '<span class="err">(rejected by OLT)</span>'}</summary><pre class="out">${esc(x.output)}</pre></details>`).join("");
    } catch (e) { $("#d-diag").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
    ev.target.disabled = false;
  };
  $("#d-sync").onclick = async () => { try { await api(`/api/olts/${id}/sync`, {method: "POST"}); toast("Sync scheduled"); } catch (e) { toast(e.message, true); } };
  $("#d-save").onclick = async ev => { ev.target.disabled = true; try { const r = await api(`/api/olts/${id}/save-config`, {method: "POST"}); toast(r.ok ? "Config saved" : "Failed: " + r.message, !r.ok); tDetails(); } catch (e) { toast(e.message, true); ev.target.disabled = false; } };
}
async function tCards() {
  const el = $("#tab-cards");
  const draw = r => { el.innerHTML = `<div class="row gap pad"><button class="primary" id="c-ref">Refresh OLT card info</button><span class="muted small">Info updated: ${r.updated_at ? ago(r.updated_at) : "never"}</span></div>
    <table><thead><tr><th>Slot</th><th>Type</th><th>Real type</th><th>Ports</th><th>SW</th><th>Status</th><th>Role</th></tr></thead><tbody>
    ${r.rows.map(c => `<tr><td>${c.slot}</td><td>${esc(c.type)}</td><td>${esc(c.real_type)}</td><td>${c.ports ?? "—"}</td><td>${esc(c.sw) || "—"}</td>
      <td class="${c.online === false ? "err" : ""}">${c.online === null ? "—" : c.online ? "Online" : "Offline"} <span class="muted small">${esc(c.status)}</span></td><td>${esc(c.role) || "—"}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">No data yet. Click Refresh.</td></tr>'}
    </tbody></table>${raw(r.raw)}`;
    $("#c-ref").onclick = async ev => { ev.target.disabled = true; try { const x = await api(`/api/olts/${id}/cards/refresh`, {method: "POST"}); if (!x.ok) toast("The OLT rejected the 'show card' command", true); draw(x); } catch (e) { toast(e.message, true); ev.target.disabled = false; } };
  };
  draw(await api(`/api/olts/${id}/cards`));
}
async function tPon() {
  const el = $("#tab-pon"), r = await api(`/api/olts/${id}/pon-ports`);
  el.innerHTML = `<div class="muted small pad">ONU count, load, and average signal are calculated from ONU data; descriptions and admin state come from running-config. Link status and Tx power are not available.</div>` +
    (r.slots.map(s => `<h4 class="slot">OLT slot ${s.slot}${s.board_type ? `, board type: ${esc(s.board_type)}` : ""}</h4><table><thead><tr><th>Port</th><th>Type</th><th>Admin state</th><th>Status</th><th>ONUs</th><th>Load</th><th>Average signal</th><th>Description</th><th>Action</th></tr></thead><tbody>
    ${s.ports.map(p => `<tr><td>${p.port}</td><td>${p.type}</td><td class="${p.admin === "Disabled" ? "err" : ""}">${p.admin}</td><td>${p.status || "-"}</td>
      <td>Online: ${p.online}<br>Total: ${p.total}</td><td><span class="usage"><i style="width:${Math.min(100, p.total / p.capacity * 100)}%"></i></span> ${p.total}/${p.capacity}</td>
      <td>${p.avg_rx == null ? "—" : rxbar(p.avg_rx)}</td><td>${esc(p.description)}</td>
      <td><a href="/graphs?olt=${id}&tab=pon&key=${encodeURIComponent("gpon-olt_" + p.shelf + "/" + p.slot + "/" + p.port)}">Traffic</a></td></tr>`).join("")}      </tbody></table>`).join("") || '<p class="muted">No PON data yet. Wait for the first sync.</p>');
}
const later = fn => setTimeout(fn, 7000);   // The worker re-reads OLT config a few seconds after changes.
async function tUplink() {
  const el = $("#tab-uplink");
  const draw = r => {
    const names = r.rows.filter(u => /^x?gei/i.test(u.name)).map(u => [u.name, u.name]);
    el.innerHTML = `<div class="row gap pad"><button class="primary" id="u-ref">Refresh uplink ports info</button>
        <button id="u-add">Add VLAN tag</button><span class="muted small">Refresh also reads optical power (Rx/Tx/temperature) for each port.</span></div>
        ${r.live_error ? `<p class="err small">Some live data could not be retrieved: ${esc(r.live_error)}</p>` : ""}
      <table><thead><tr><th>Uplink port</th><th>Description</th><th>Admin state</th><th>Status</th><th>Speed</th><th>MTU</th><th>WaveL</th><th>Signal dBm</th><th>Temp</th><th>Mode: tagged VLANs</th><th>PVID</th><th>Action</th></tr></thead><tbody>
      ${r.rows.map(u => `<tr><td>${esc(u.name)}</td><td>${esc(u.description)}</td><td class="${u.admin === "Disabled" ? "err" : ""}">${esc(u.admin) || "—"}</td>
        <td class="${u.status === "Down" ? "err" : ""}">${u.status || "—"}</td><td>${u.speed_mbps != null ? u.speed_mbps + " Mbps" : (esc(u.speed) || "—")}</td><td>${u.mtu ?? "—"}</td>
        <td>${u.wavelength ?? "—"}</td><td>${u.rx != null ? `Tx: ${u.tx}<br>Rx: ${u.rx}` : "—"}</td><td>${u.temp ?? "—"}</td>
        <td class="wrap">${u.mode ? esc(u.mode[0].toUpperCase() + u.mode.slice(1)) + ": " : ""}${esc(u.vlans) || "—"}</td><td>${esc(u.pvid) || "—"}</td>
        <td><a href="/graphs?olt=${id}&tab=uplink&key=${encodeURIComponent(u.name)}">Traffic</a></td></tr>`).join("") || '<tr><td colspan="12" class="muted">No uplink data available.</td></tr>'}</tbody></table>`;
    $("#u-ref").onclick = async ev => { ev.target.disabled = true; try { draw(await api(`/api/olts/${id}/uplinks?live=1&optical=1`)); } catch (e) { toast(e.message, true); ev.target.disabled = false; } };
    $("#u-add").onclick = async () => {
      const v = await askForm({title: "VLAN tag on uplink port", fields: [
        {k: "port", label: "Uplink port", type: "select", options: names}, {k: "vlans", label: "VLAN (e.g. 700 or 700,710-712)", placeholder: "700"},
        {k: "op", label: "Action", type: "select", options: [["add", "Add tag"], ["remove", "Remove tag"]]}], submit: "Preview commands"});
      if (v) openPlan({action: "uplink_vlan", olt_id: id, params: v, onDone: () => later(() => { loaded.uplink = false; tUplink(); })});
    };
  };
  draw(await api(`/api/olts/${id}/uplinks?live=1`));
}
async function tVlans() {
  const el = $("#tab-vlans"), r = await api(`/api/olts/${id}/vlans`);
  el.innerHTML = `<div class="row gap pad"><button class="primary" id="v-add">Add VLAN</button><span class="muted small">From running-config and VLANs used by ONUs.</span></div>
    <table><thead><tr><th>VLAN-ID</th><th>Description</th><th>ONUs</th><th>In OLT config</th></tr></thead><tbody>
    ${r.rows.map(v => `<tr><td>${v.vlan}</td><td>${esc(v.name) || "—"}</td><td>${v.onus ? `<a href="/configured?olt=${id}&vlan=${v.vlan}">${v.onus}</a>` : 0}</td><td>${v.in_config ? "Yes" : '<span class="muted">not detected</span>'}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">No VLAN data yet.</td></tr>'}</tbody></table>`;
  $("#v-add").onclick = async () => {
    const ups = (await api(`/api/olts/${id}/uplinks`)).rows.filter(u => /^x?gei/i.test(u.name) && u.admin !== "Disabled").map(u => [u.name, u.name + (u.description ? ` (${u.description})` : "")]);
    const v = await askForm({title: "Add VLAN to OLT", note: "The VLAN is created in the OLT database; select uplink ports to tag it as well (configure downlinks/ONUs under Configure Ethernet port).",
      fields: [{k: "vlans", label: "VLAN-ID (mis. 700 atau 700,710-712)", placeholder: "700"},
      {k: "name", label: "Name (optional; for a single VLAN only)", req: false},
      {k: "uplink_ports", label: "Tag on uplink ports", type: "checks", options: ups}], submit: "Preview commands"});
    if (v) openPlan({action: "vlan_add", olt_id: id, params: {...v, uplink_ports: v.uplink_ports ? v.uplink_ports.split(",") : []}, onDone: () => later(() => { loaded.vlans = false; tVlans(); })});
  };
}
async function tAdvanced() {
  const el = $("#tab-advanced"), s = await api(`/api/olts/${id}/settings`);
  el.innerHTML = `<div class="infogrid">
    <label><span>Signal RX warning (dBm)</span><input id="a-w" type="number" step="0.5" value="${s.rx_warn}"></label>
    <label><span>Signal RX critical (dBm)</span><input id="a-c" type="number" step="0.5" value="${s.rx_crit}"></label>
    <label><span>Temperature warning (°C)</span><input id="a-tw" type="number" value="${s.temp_warn}"></label>
    <label><span>Temperature critical (°C)</span><input id="a-tc" type="number" value="${s.temp_crit}"></label></div>
    <p class="muted small">Signal thresholds control colors and the Signal filter (Good/Warning/Critical) in Configured & Diagnostics. Other options (DHCP option 82, PPPoE-plus, MAC list, etc.) require OLT writes and are not available yet.</p>
    <div class="row end"><button class="primary" id="a-save">Save</button></div>`;
  $("#a-save").onclick = async () => { try { await api(`/api/olts/${id}/settings`, {method: "PUT", body: {rx_warn: +$("#a-w").value, rx_crit: +$("#a-c").value, temp_warn: +$("#a-tw").value, temp_crit: +$("#a-tc").value}}); toast("Saved"); } catch (e) { toast(e.message, true); } };
}
const loaders = {details: tDetails, cards: tCards, pon: tPon, uplink: tUplink, vlans: tVlans, advanced: tAdvanced};
async function show(tab) {
  $$("#tabs button[data-tab]").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach(s => s.hidden = s.id !== "tab-" + tab);
  if (tab === "uplink" || !loaded[tab]) { loaded[tab] = true; try { await loaders[tab](); } catch (e) { fail($("#tab-" + tab), e); loaded[tab] = false; } }
}
$("#tabs").onclick = e => { const b = e.target.closest("button[data-tab]"); if (b) show(b.dataset.tab); };
onEvent(m => { if (m.type === "olt" && m.id === id && !$("#tab-details").hidden) tDetails(); });
show("details");
