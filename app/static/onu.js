const id = window.ONU_ID, loaded = {};
let timer = null, last = null, series = {in: [], out: []}, signalSeries = [], statusRequest = 0;
let currentOnu = null;
let headerWanRefreshInProgress = false;
let headerRxWarn = -30, headerRxCrit = -32;
const fmtBps = v => v == null ? "—" : v >= 1e9 ? (v / 1e9).toFixed(2) + " Gbps" : v >= 1e6 ? (v / 1e6).toFixed(2) + " Mbps" : v >= 1e3 ? (v / 1e3).toFixed(1) + " kbps" : v + " bps";
const dbm = v => v == null ? "—" : v.toFixed(2) + " dBm";
const pre = t => `<pre class="out">${esc(t || "(empty output)")}</pre>`;
function updateHeaderStatus(status, phase, optical) {
  if (!$("#onu-header-badge")) return;
  $("#onu-header-badge").innerHTML = badge(status);
  $("#onu-header-phase").textContent = phase || "—";
  $("#onu-header-rx").innerHTML =
    `${rx(opticalValue(status, optical.onu_rx), headerRxWarn, headerRxCrit)} / ${rx(opticalValue(status, optical.olt_rx), headerRxWarn, headerRxCrit)} dBm`;
  recordSignal(opticalValue(status, optical.olt_rx));
}

function recordSignal(value) {
  if (value == null) return;
  signalSeries.push({value, time: Date.now()});
  signalSeries = signalSeries.slice(-60);
  renderSignal();
}

function renderSignal() {
  if (!$("#signal-current")) return;
  const latest = signalSeries.at(-1).value, peak = Math.max(...signalSeries.map(p => p.value));
  $("#signal-current").textContent = `${latest.toFixed(2)} dBm`;
  $("#signal-peak").textContent = `${peak.toFixed(2)} dBm`;
  drawSignal();
}

function drawSignal() {
  const cv = $("#signal-cv");
  if (!cv) return;
  const dpr = devicePixelRatio || 1, w = cv.clientWidth * dpr, h = cv.clientHeight * dpr;
  cv.width = w; cv.height = h;
  const g = cv.getContext("2d");
  g.clearRect(0, 0, w, h);
  if (signalSeries.length < 1) return;
  const values = signalSeries.map(p => p.value), min = Math.min(...values) - 2, max = Math.max(...values) + 2;
  g.strokeStyle = "#d97706"; g.lineWidth = 2 * dpr; g.beginPath();
  signalSeries.forEach((p, i) => {
    const x = i / Math.max(1, signalSeries.length - 1) * w;
    const y = h - (p.value - min) / (max - min) * (h - 10 * dpr) - 5 * dpr;
    i ? g.lineTo(x, y) : g.moveTo(x, y);
  });
  g.stroke();
}

async function refreshHeaderStatus() {
  const r = await api(`/api/onus/${id}/status?summary=true`);
  updateHeaderStatus(r.status, r.phase_state, r.optical);
  pollTraffic.lastSignal = Date.now();
}

async function header() {
  const editable = window.USER_ROLE === "administrator";
  const oltLimited = window.USER_ROLE === "olt_limited";
  const o = await api(`/api/onus/${id}`);
  currentOnu = o;
  if (o.olt.vendor === "cdata") {
    headerRxWarn = o.olt.rx_warn ?? -30;
    headerRxCrit = o.olt.rx_crit ?? -32;
    $$("#tabs button[data-tab]").forEach(b => { b.hidden = b.dataset.tab !== "status"; });
    $$("#tabs button[data-act]").forEach(b => { b.hidden = true; });
    ["tab-config", "tab-sw", "tab-traffic", "tab-eth", "speed-profiles", "wifi-panel"]
      .forEach(selector => { const section = $("#" + selector); if (section) section.hidden = true; });
    $("#info").innerHTML = `<div class="infogrid">
      <div class="onu-info-col"><label><span>OLT</span>${esc(o.olt.name)}</label>
        <label><span>Vendor / model</span>C-DATA ${esc(o.olt.model)}</label>
        <label><span>EPON port / ONU</span>${esc(o.interface)}</label>
        <label><span>ONU MAC</span>${esc(o.sn || "—")}</label></div>
      <div class="onu-info-col"><label><span>Status</span><span class="onu-header-status"><span id="onu-header-badge">${badge(o.status)}</span> <small id="onu-header-phase" class="muted">${esc(o.phase_state)}</small></span></label>
        <label><span>ONU Rx</span><b id="onu-header-rx">${rx(opticalValue(o.status, o.rx_power), headerRxWarn, headerRxCrit)} / — dBm</b></label>
        <label><span>WAN IP / Remote Web</span><span id="onu-wan-ip" class="muted small">Checking active PPPoE…</span></label>
        <label><span>PPPoE username</span><code id="onu-pppoe-user" data-configured="">—</code></label>
        <label><span>Integration mode</span>Read-only SNMP monitoring</label></div></div>`;
    show("status");
    loadHeaderWanIp();
    return;
  }
  if (oltLimited) {
    headerRxWarn = o.olt.rx_warn ?? -30;
    headerRxCrit = o.olt.rx_crit ?? -32;
    const canReboot = !!o.olt.write_enabled;
    $$("#tabs button[data-tab]").forEach(b => { b.hidden = b.dataset.tab !== "status"; });
    $$("#tabs button[data-act]").forEach(b => { b.hidden = b.dataset.act !== "reboot" || !canReboot; });
    ["tab-config", "tab-sw", "tab-traffic", "tab-eth", "speed-profiles", "wifi-panel"]
      .forEach(selector => { const section = $("#" + selector); if (section) section.hidden = true; });
    $("#info").innerHTML = `<div class="infogrid">
      <div class="onu-info-col"><label><span>OLT</span>${esc(o.olt.name)}</label>
        <label><span>Port / ONU</span>${esc(o.interface)}</label>
        <label><span>Name</span>${esc(o.name || "—")}</label>
        <label><span>SN / MAC</span>${esc(o.sn || "—")}</label>
        <label><span>ONU type</span>${esc(o.onu_type || "—")}</label></div>
      <div class="onu-info-col"><label><span>Status</span><span class="onu-header-status"><span id="onu-header-badge">${badge(o.status)}</span> <small id="onu-header-phase" class="muted">${esc(o.phase_state)}</small></span></label>
        <label><span>ONU Rx</span><b id="onu-header-rx">${rx(opticalValue(o.status, o.rx_power), headerRxWarn, headerRxCrit)} / — dBm</b></label></div></div>`;
    show("status");
    return;
  }
  if (o.olt.vendor === "hioso") {
    headerRxWarn = o.olt.rx_warn ?? -30;
    headerRxCrit = o.olt.rx_crit ?? -32;
    const hiosoActionsEnabled = editable && o.olt.write_enabled;
    const hiosoRebootEnabled = hiosoActionsEnabled;
    $$("#tabs button[data-tab]").forEach(b => { b.hidden = b.dataset.tab !== "status"; });
    $$("#tabs button[data-act]").forEach(b => { b.hidden = b.dataset.act !== "reboot" || !hiosoRebootEnabled; });
    $("#speed-profiles").hidden = true; $("#wifi-panel").hidden = true;
    $("#info").innerHTML = `<div class="infogrid">
      <div class="onu-info-col"><label><span>OLT</span><a href="/olts/${o.olt.id}">${esc(o.olt.name)}</a></label>
        <label><span>Vendor / model</span>${esc(o.olt.vendor.toUpperCase())} ${esc(o.olt.model)}</label>
        <label><span>EPON port / ONU</span>${esc(o.interface)}</label>
        <label><span>Name</span>${hiosoActionsEnabled
        ? `<span class="row gap"><input id="e-name" maxlength="32" value="${esc(o.name || "")}"><button type="button" class="sm" id="b-hioso-rename">Rename ONT</button></span>`
          : esc(o.name || "—")}</label>
        <label><span>ONU MAC</span>${esc(o.sn || "—")}</label></div>
      <div class="onu-info-col"><label><span>Status</span><span class="onu-header-status"><span id="onu-header-badge">${badge(o.status)}</span> <small id="onu-header-phase" class="muted">${esc(o.phase_state)}</small></span></label>
        <label><span>ONU Rx</span><b id="onu-header-rx">${rx(opticalValue(o.status, o.rx_power), headerRxWarn, headerRxCrit)} / — dBm</b></label>
        <label><span>WAN IP / Remote Web</span><span id="onu-wan-ip" class="muted small">Checking active PPPoE…</span></label>
        <label><span>PPPoE username</span><code id="onu-pppoe-user" data-configured="${esc(o.pppoe_user || "")}">${esc(o.pppoe_user || "—")}</code></label>
        <label><span>Integration mode</span>${hiosoActionsEnabled
          ? "SNMP monitoring + Telnet detail + confirmed rename/reboot"
          : o.olt.telnet_configured ? "SNMP monitoring + read-only Telnet detail" : "SNMP read-only"}</label></div></div>`;
    if (hiosoActionsEnabled) {
      $("#e-name").title = "Up to 32 characters. Spaces and unsupported characters are converted to underscores.";
      $("#b-hioso-rename").onclick = async () => {
        const name = $("#e-name").value.trim();
        if (!name) return toast("Enter a name for this ONU.", true);
        if (!confirm(`Rename ONT ${o.interface} to "${name}" on ${o.olt.name}?`)) return;
        const button = $("#b-hioso-rename");
        button.disabled = true;
        try {
          await api(`/api/onus/${id}/hioso/rename`, {method: "POST", body: {name, confirm: true}});
          toast("ONT renamed on the HIOSO OLT.");
          try { await header(); }
          catch (e) { toast(`ONT was renamed, but the page could not refresh: ${e.message}`, true); }
        } catch (e) { toast(e.message, true); }
        finally { if ($("#b-hioso-rename")) $("#b-hioso-rename").disabled = false; }
      };
    }
    show("status");
    loadHeaderWanIp();
    return;
  }
  const f = await api("/api/filters");
  const types = await api(`/api/onu-types?q=${encodeURIComponent(o.onu_type)}`);
  headerRxWarn = o.olt.rx_warn ?? -30;
  headerRxCrit = o.olt.rx_crit ?? -32;
  const opt = (arr, cur) => '<option value="">—</option>' + arr.map(x => `<option value="${x.id}" ${x.id === cur ? "selected" : ""}>${esc(x.name)}</option>`).join("");
  const prof = d => f.profiles.filter(p => p.direction === d);
  $("#info").innerHTML = `<div class="infogrid">
    <div class="onu-info-col">
      <label><span>OLT</span><b>${editable ? `<a href="/olts/${o.olt.id}">${esc(o.olt.name)}</a>` : esc(o.olt.name)}</b></label>
      <label><span>Board</span>${o.slot}</label>
      <label><span>Port</span>${o.pon}</label>
      <label><span>Mgmt IP</span>${o.mgmt_ip ? esc(o.mgmt_ip) + " (vlan " + o.mgmt_vlan + ")" : "—"}</label>
      <label><span>ONU mode</span><b>${o.mode === "routing" ? "Routing" : o.mode === "bridge" ? "Bridging" : "—"}${o.mode === "routing" && o.vlan ? " - WAN VLAN: " + o.vlan : ""}</b> ${editable ? '<button type="button" class="sm" id="b-mode">Update ONU mode</button>' : ""}</label>
      <label><span>WAN setup mode</span>${esc((o.conn_type || "—").toUpperCase())}</label>
      <label><span>Zone</span>${editable ? `<select id="e-zone">${opt(f.zones, o.zone_id)}</select>` : esc(o.zone || "—")}</label>
      <label><span>ODB (Splitter)</span>${editable ? `<select id="e-odb">${opt(f.odbs, o.odb_id)}</select>` : esc(o.odb || "—")}</label>
      <label><span>Contact</span>${editable ? `<input id="e-contact" maxlength="100" value="${esc(o.contact)}">` : esc(o.contact || "—")}</label>
      <label><span>ONU external ID</span>${editable ? `<input id="e-ext" maxlength="64" value="${esc(o.external_id)}">` : esc(o.external_id || "—")}</label>
    </div>
    <div class="onu-info-col">
      <label><span>Status</span><span class="onu-header-status"><span id="onu-header-badge">${badge(o.status)}</span> <small id="onu-header-phase" class="muted">${esc(o.phase_state)}</small></span></label>
      <label><span>ONU/OLT Rx signal</span><b id="onu-header-rx">${rx(opticalValue(o.status, o.rx_power), o.olt.rx_warn, o.olt.rx_crit)} / ${rx(opticalValue(o.status, o.olt_rx_power), o.olt.rx_warn, o.olt.rx_crit)} dBm</b></label>
      <label><span>Attached VLANs</span>${o.vlan ?? "—"}</label>
      <label><span>ONU</span>${esc(o.interface)}</label>
      <label><span>SN</span>${esc(o.sn)} ${editable ? '<button type="button" class="sm" id="b-replace-sn">Replace SN</button>' : ""}</label>
      <label><span>ONU type</span>${esc(o.onu_type)}</label>
      <label><span>WAN IP / Remote Web</span><span id="onu-wan-ip" class="muted small">Checking active PPPoE…</span></label>
      <label><span>PPPoE username</span><code id="onu-pppoe-user" data-configured="${esc(o.pppoe_user || "")}">${esc(o.pppoe_user || "—")}</code> ${editable && o.mode === "routing" ? '<button type="button" class="sm" id="b-pppoe">Edit credentials</button>' : ""}</label>
      <label><span>Distance</span>${o.distance_m == null ? "—" : o.distance_m + " m"}</label>
      <label><span>Authorization date</span>${fmtDate(o.auth_date)}</label>
      <label><span>Name</span>${editable ? `<input id="e-name" maxlength="64" value="${esc(o.name)}">` : esc(o.name || "—")}</label>
      <label><span>Address or comment</span>${editable ? `<input id="e-addr" maxlength="300" value="${esc(o.address)}">` : esc(o.address || "—")}</label>
    </div>
  </div>
    ${editable ? `<div class="row between pad"><span class="muted small">${o.olt.cli_family === "c6xx" ? "For C600/C6xx, name and description are stored in the dashboard because the OLT may mask them during synchronization." : "Name changes are sent to the OLT after confirmation."} Zone, ODB, address, contact, and speed profiles are stored only in the dashboard.</span>
      <span class="row gap"><button class="primary" id="e-save">Save</button></span></div>` : ""}`;
  $("#speed-profiles").innerHTML = editable ? `<h3>Speed profiles</h3>
    <div class="scroll"><table><thead><tr><th>Download</th><th>Upload</th><th>Action</th></tr></thead><tbody><tr>
      <td><select id="e-dl">${opt(prof("download"), o.dl_profile_id)}</select></td>
      <td><select id="e-ul">${opt(prof("upload"), o.ul_profile_id)}</select></td>
      <td><button id="e-apply">Apply speed to OLT</button></td>
    </tr></tbody></table></div>`
    : `<h3>Speed profiles</h3><p>Download: ${esc(o.dl_profile || "—")} · Upload: ${esc(o.ul_profile || "—")}</p>`;
  const onuType = types.find(t => t.name.toLowerCase() === o.onu_type.toLowerCase());
  const wifiCount = onuType?.wifi_ports ?? 0;
  $("#wifi-panel").innerHTML = `<h3>WiFi</h3>
    ${wifiCount ? `<p class="muted">Type ${esc(o.onu_type)} supports ${wifiCount} WiFi ports.</p>` :
      `<p class="muted">No WiFi ports are recorded for type ${esc(o.onu_type || "this ONU")}.</p>`}
    <div class="scroll"><table><thead><tr><th>Port</th><th>Admin state</th><th>Mode</th><th>SSID</th><th>DHCP</th><th>Action</th></tr></thead>
      <tbody>${wifiCount ? Array.from({length: wifiCount}, (_, i) =>
        `<tr><td>wifi_0/${i + 1}</td><td>—</td><td>—</td><td>—</td><td>—</td><td><span class="muted">Unavailable</span></td></tr>`).join("") :
        '<tr><td colspan="6" class="muted">No WiFi ports are recorded for this type.</td></tr>'}</tbody></table></div>
    <p class="small muted">Radio status, SSID, DHCP, and WiFi configuration are not available through the current OLT integration.</p>`;
  if (!editable) return;
  $("#b-replace-sn").onclick = async buttonEvent => {
    const button = buttonEvent.currentTarget;
    const values = await askForm({title: "Replace ONU serial number", submit: "Continue",
      note: `Enter the new ONU SN manually. The OLT will briefly remove and re-register ONU ${o.slot}/${o.pon} at the same ONU index, then restore its captured service configuration. The service may be interrupted briefly.`,
      fields: [{k: "sn", label: "New ONU SN"}]});
    if (!values) return;
    const dialog = $("#plan-dialog");
    if (dialog.open) dialog.close();
    const newSn = values.sn.trim().toUpperCase();
    if (!confirm(`Replace ONU SN ${o.sn} with ${newSn} at ${o.olt.name} ${o.slot}/${o.pon}? The ONU will be temporarily removed and re-registered. Its captured service configuration will be restored; service may be interrupted briefly.`)) return;
    button.disabled = true;
    const originalLabel = button.textContent;
    try {
      button.textContent = "Replacing…";
      const job = await api(`/api/onus/${id}/replace-sn`, {method: "POST", body: {sn: newSn, confirm: true}});
      toast(`Replace SN started in the background for ${job.old_sn} → ${job.sn}.`);
      let result;
      do {
        await new Promise(resolve => setTimeout(resolve, 2000));
        result = await api(`/api/onu-replacements/${job.id}`);
        button.textContent = result.status === "queued" ? "Queued…" : "Replacing…";
      } while (result.status === "queued" || result.status === "running");
      if (result.status !== "completed") throw new Error(result.message || "Replace SN failed.");
      toast(`ONU replaced: ${result.old_sn} → ${result.sn}; service configuration restored.`);
      if (!result.saved) toast(`SN updated on the OLT, but saving to OLT memory failed: ${result.save_message}`, true);
      await header();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = originalLabel;
    }
  };
  if ($("#b-pppoe")) $("#b-pppoe").onclick = async () => {
    const v = await askForm({title: "Update PPPoE credentials", submit: "Update", fields: [
      {k: "username", label: "Username", value: o.pppoe_user || ""},
      {k: "password", label: "Password", type: "password", note: "Sent to the OLT; not stored in the dashboard."}]});
    if (v) executeAction({action: "pppoe_credentials", onu_id: id, params: v,
      title: "PPPoE credentials update", button: $("#b-pppoe"), onDone: () => header()});
  };
  $("#b-mode").onclick = async () => {
    const vl = await api(`/api/olts/${o.olt.id}/vlans`);
    const opts = vl.rows.filter(v => v.vlan > 1 && (v.name || v.onus)).map(v => [v.vlan, `${v.vlan}${v.name ? " - " + v.name : ""}`]);
    if (o.vlan && !opts.some(x => x[0] === o.vlan)) opts.unshift([o.vlan, String(o.vlan)]);
    const routing = v => v.mode === "routing";
    const routingSetup = v => routing(v) && o.mode !== "routing";
    const v = await askForm({title: "Update ONU mode", submit: "OK",
      note: "Changing only the VLAN while keeping Routing preserves current PPPoE credentials and GEM port. Switching to Routing from Bridging requires PPPoE credentials. OMCI + IPv4 only; WAN remote access, DHCP/Static IP, TR-069, and dual stack are not supported yet.", fields: [
      {k: "wan_vlan", label: "WAN VLAN-ID", type: "select", value: o.vlan, options: opts.length ? opts : [["", "(no VLANs available)"]]},
      {k: "mode", label: "ONU mode", type: "select", value: o.mode || "routing", options: [["routing", "Routing"], ["bridge", "Bridging"]]},
      {k: "wan_mode", label: "WAN mode", type: "select", value: "pppoe", when: routingSetup, options: [["pppoe", "PPPoE"], ["dhcp", "DHCP (not supported)", true], ["static", "Static IP (not supported)", true]]},
      {k: "method", label: "Config method", type: "select", value: "omci", when: routingSetup, options: [["omci", "OMCI"], ["tr069", "TR-069 (not supported)", true]]},
      {k: "ip_protocol", label: "IP Protocol", type: "select", value: "ipv4", when: routingSetup, options: [["ipv4", "IPv4"], ["dual", "Dual stack IPv4/IPv6 (not supported)", true]]},
      {k: "username", label: "Username", value: o.pppoe_user || "", when: routingSetup},
      {k: "password", label: "Password", type: "password", when: routingSetup, note: "Sent to the OLT; not stored in the dashboard."}]});
    if (v) {
      $("#plan-dialog").close();
      await executeAction({action: "onu_mode", onu_id: id, params: {...v, wan_vlan: +v.wan_vlan},
        title: "ONU mode update", button: $("#b-mode"), onDone: () => header()});
    }
  };
  $("#e-apply").onclick = () => {
    const dl = +$("#e-dl").value, ul = +$("#e-ul").value;
    if (!dl || !ul) return toast("Select download and upload speed profiles", true);
    executeAction({action: "apply_speed", onu_id: id, params: {dl_profile_id: dl, ul_profile_id: ul},
      title: "Speed profile update", button: $("#e-apply"), onDone: () => header()});
  };
  $("#e-save").onclick = async () => {
    const n = v => v ? +v : null;
    const name = $("#e-name").value.trim();
    const data = {name, address: $("#e-addr").value, contact: $("#e-contact").value,
      external_id: $("#e-ext").value, zone_id: n($("#e-zone").value), odb_id: n($("#e-odb").value),
      dl_profile_id: n($("#e-dl").value), ul_profile_id: n($("#e-ul").value)};
    const save = async () => api(`/api/onus/${id}`, {method: "PUT", body: data});
    if (name.trim() !== (o.name || "").trim()) {
      if (o.olt.cli_family === "c6xx") {
        try {
          await save();
          toast("Saved in dashboard database");
          await header();
        } catch (e) { toast(e.message, true); }
        return;
      }
      executeAction({action: "rename_onu", onu_id: id, params: {name}, title: "Rename ONU", button: $("#e-save"), onDone: async () => {
        try {
          await save();
        } catch (e) { toast(`The OLT name was changed, but the dashboard data could not be saved: ${e.message}`, true); return; }
        try { await header(); }
        catch (e) { toast(`The change was saved, but the page could not be refreshed: ${e.message}`, true); }
      }});
      return;
    }
    try {
      await save();
      toast("Saved");
    } catch (e) { toast(e.message, true);     }
  };
  refreshHeaderStatus().catch(e => toast(`Live status refresh failed: ${e.message}`, true));
  loadHeaderWanIp();
}
function loading(el, msg = "Retrieving data from OLT…") { el.innerHTML = `<p class="muted">${msg}</p>`; }
function failed(el, e, retry) { el.innerHTML = `<p class="err">${esc(e.message)}</p><button class="sm" id="retry">Retry</button>`; $("#retry", el).onclick = retry; }

async function tabStatus() {
  const el = $("#tab-status"); loading(el);
  const request = ++statusRequest;
  try {
    const r = await api(`/api/onus/${id}/status?summary=true`), o = r.optical;
    updateHeaderStatus(r.status, r.phase_state, o);
    if (currentOnu?.olt.vendor === "cdata") {
      const fields = Object.entries(r.fields || {}).map(([key, value]) =>
        `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("");
      el.innerHTML = `<div class="row between"><b>${esc(r.interface)}</b><button class="sm" id="r1">Refresh</button></div>
        <div class="big"><div class="tile"><b>${badge(r.status)}</b><span>Online Status</span></div>
        <div class="tile"><b>${dbm(opticalValue(r.status, o.onu_rx))}</b><span>ONU Rx Power</span></div></div>
        ${fields ? `<h4 class="slot">ONU detail from C-Data</h4><dl class="kv">${fields}</dl>` : ""}
        ${r.error ? `<p class="err small">ONU details unavailable: ${esc(r.error)}</p>` : ""}
        <p class="muted small">Only ONU identity, link status, and Rx power are currently verified for this model. Configuration changes and other optical metrics are unavailable.</p>`;
      $("#r1").onclick = tabStatus;
      return;
    }
    if (currentOnu?.olt.vendor === "hioso") {
      const fields = Object.entries(r.fields || {}).map(([key, value]) =>
        `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("");
      el.innerHTML = `<div class="row between"><b>${esc(r.interface)}</b><button class="sm" id="r1">Refresh</button></div>
        <div class="big"><div class="tile"><b>${badge(r.status)}</b><span>Online Status</span></div>
        <div class="tile"><b>${dbm(opticalValue(r.status, o.onu_rx))}</b><span>ONU Rx Power</span></div></div>
        ${fields ? `<h4 class="slot">ONU detail from HIOSO</h4><dl class="kv">${fields}</dl>` : ""}
        ${r.error ? `<p class="err small">Additional ONU details unavailable: ${esc(r.error)}</p>` : ""}
        <p class="muted small">This firmware's verified detail command does not report offline reason, ONU temperature/voltage/bias, or ONU Tx power. Configuration changes other than the separately confirmed rename/reboot are unavailable.</p>`;
      $("#r1").onclick = tabStatus;
      return;
    }
    const kv = Object.entries(r.fields).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    el.innerHTML = `<div class="row between"><b>${esc(r.interface)}</b><button class="sm" id="r1">Refresh</button></div>
    <div class="big">
      <div class="tile"><b>${badge(r.status)}</b><span>Phase: ${esc(r.phase_state || "—")}</span></div>
      <div class="tile"><b>${dbm(opticalValue(r.status, o.onu_rx))}</b><span>ONU Rx</span></div>
      <div class="tile"><b>${dbm(opticalValue(r.status, o.olt_rx))}</b><span>OLT Rx</span></div>
      <div class="tile"><b>${dbm(opticalValue(r.status, o.onu_tx))}</b><span>Tx ONU</span></div>
      <div class="tile"><b>${opticalValue(r.status, o.att_down) == null ? "—" : o.att_down.toFixed(1) + " dB"}</b><span>Downstream attenuation</span></div>
      <div class="tile"><b>${opticalValue(r.status, o.att_up) == null ? "—" : o.att_up.toFixed(1) + " dB"}</b><span>Upstream attenuation</span></div>
    </div>
    ${r.status === "unknown" ? '<p class="err">Could not read the phase state from OLT output — see the raw output below.</p>' : ""}
    ${kv ? `<dl class="kv">${kv}</dl>` : ""}
    <section id="status-mac"><h4 class="slot">MAC address</h4><p class="muted small">Loading MAC table…</p></section>
    ${r.history && r.history.length ? `<h4 class="slot">ONU history (last 10)</h4><table><thead><tr><th>#</th><th>Authpass</th><th>Offline</th><th>Cause</th></tr></thead><tbody>${r.history.map(h => `<tr><td>${h.n}</td><td>${esc(h.authpass)}</td><td>${h.offline ? esc(h.offline) : '<span class="muted">online now</span>'}</td><td>${esc(h.cause) || "—"}</td></tr>`).join("")}</tbody></table>` : ""}
    <details ${kv ? "" : "open"}><summary>Raw OLT output</summary>${pre(r.raw.detail)}${pre(r.raw.attenuation)}<div id="status-mac-raw"></div></details>`;
    $("#r1").onclick = tabStatus;
    requestAnimationFrame(() => loadStatusMac(request));
  } catch (e) { failed(el, e, tabStatus); }
}

async function loadStatusMac(request) {
  const section = $("#status-mac");
  if (!section || request !== statusRequest) return;
  try {
    const mac = await api(`/api/onus/${id}/mac`);
    if (request !== statusRequest || !$("#status-mac")) return;
    const rows = mac.rows.length
      ? `<table><thead><tr><th>MAC address</th><th>VLAN</th><th>Type</th><th>Details</th></tr></thead><tbody>${mac.rows.map(m =>
          `<tr><td><code class="mac">${esc(m.mac)}</code></td><td>${m.vlan ?? "—"}</td><td>${esc(m.type) || "—"}</td><td class="muted">${esc(m.info) || "—"}</td></tr>`).join("")}</tbody></table>`
      : mac.ok ? '<p class="muted">No MAC addresses detected (ONU offline or no traffic yet).</p>'
        : '<p class="err">The OLT rejected the MAC command — syntax may differ in your firmware (change <code>mac_cmd</code> in data/profiles.json).</p>';
    section.innerHTML = `<h4 class="slot">MAC address <span class="muted small">(ONT &amp; connected devices — <code>${esc(mac.command || "show mac gpon onu")}</code>)</span></h4>${rows}`;
    $("#status-mac-raw").innerHTML = pre(mac.raw);
  } catch (e) {
    if (request === statusRequest && $("#status-mac")) {
      section.innerHTML = `<h4 class="slot">MAC address</h4><p class="err">${esc(e.message)}</p>`;
    }
  }
}

async function loadHeaderWanIp() {
  const display = $("#onu-wan-ip");
  if (!display) return;
  if (window.onuWanIpTimer) clearInterval(window.onuWanIpTimer);
  try {
    await refreshHeaderWanIpsFromOlt();
    window.onuWanIpTimer = setInterval(refreshHeaderWanIpsFromOlt, 60000);
  } catch (e) {
    if (!$("#onu-wan-ip")) return;
    if (!display.querySelector("a")) {
      display.className = "err small";
      display.textContent = `WAN IP lookup failed: ${e.message}`;
    }
  }
}

async function refreshHeaderWanIpsFromOlt() {
  if (headerWanRefreshInProgress) return;
  headerWanRefreshInProgress = true;
  const display = $("#onu-wan-ip");
  if (display) {
    display.className = "muted small";
    display.textContent = "Checking active PPPoE…";
  }
  try {
    if (["hioso", "cdata"].includes(currentOnu?.olt.vendor)) {
      const result = await api(`/api/mikrotik/onus/${id}/wan-ips?refresh=true`);
      if ($("#onu-wan-ip")) renderHeaderWanIps(result);
      return;
    }
    const mac = await api(`/api/onus/${id}/mac`);
    if (!display) return;
    if (!mac.ok) {
      display.className = "err small";
      display.textContent = `Could not read ONU MAC table from OLT: ${mac.raw || "command rejected"}`;
      return;
    }
    const macs = [...new Set(mac.rows.map(row => row.mac))];
    if (macs.length) {
      await refreshHeaderWanIps(macs);
      return;
    }
    const empty = await api(`/api/mikrotik/onus/${id}/wan-ips?refresh=true`);
    renderHeaderWanIps(empty);
  } catch (e) {
    if (display) {
      display.className = "err small";
      display.textContent = `WAN IP refresh failed: ${e.message}`;
    }
  } finally {
    headerWanRefreshInProgress = false;
  }
}

async function refreshHeaderWanIps(macs) {
  const display = $("#onu-wan-ip");
  if (!display || !macs.length) return;
  try {
    const query = new URLSearchParams();
    macs.forEach(mac => query.append("mac", mac));
    const result = await api(`/api/mikrotik/onus/${id}/wan-ips?${query}`);
    if ($("#onu-wan-ip")) renderHeaderWanIps(result);
  } catch (e) {
    if ($("#onu-wan-ip")) {
      display.className = "err small";
      display.textContent = `WAN IP refresh failed: ${e.message}`;
    }
  }
}

function renderHeaderWanIps(result) {
  const display = $("#onu-wan-ip");
  if (!display) return;
  const username = $("#onu-pppoe-user");
  if (result.ambiguous) {
    if (username && !username.dataset.configured) username.textContent = "Ambiguous";
    display.className = "err small";
    const details = result.errors.length ? ` Router errors: ${result.errors.join("; ")}` : "";
    display.textContent = `${result.message}${details}`;
    return;
  }
  const sessionUsers = [...new Set(result.matches.map(match => match.username?.trim()).filter(Boolean))];
  if (username && !username.dataset.configured) {
    username.textContent = sessionUsers.length ? sessionUsers.join(", ") : "—";
  }
  if (result.matches.length) {
    display.className = "";
    display.innerHTML = result.matches.map(match =>
      `<a href="${esc(match.url)}" title="${esc(match.router)} · ${esc(match.username)}">${esc(match.ip)}</a>`).join("<br>") +
      (result.errors.length ? '<br><small class="err">Some router data could not be refreshed.</small>' : "");
    return;
  }
  display.className = result.errors.length ? "err small" : "muted small";
  const details = result.errors.length ? ` Router errors: ${result.errors.join("; ")}` : "";
  display.textContent = `${result.message}${details}`;
}

onEvent(m => {
  if (m.type !== "onus") return;
  for (const c of m.changes) {
    if (c.id !== id) continue;
    updateHeaderStatus(c.status, c.phase_state, {onu_rx: c.rx_power, olt_rx: c.olt_rx_power});
  }
});

async function tabText(name, path, title) {
  const el = $("#tab-" + name); loading(el);
  try {
    const r = await api(`/api/onus/${id}/${path}`);
    el.innerHTML = `<div class="row between"><span class="muted small">Command: <code>${esc(r.command)}</code></span>
      <span class="row"><button class="sm" id="cp-${name}">Copy</button><button class="sm" id="rf-${name}">Refresh</button></span></div>
      ${r.ok ? "" : '<p class="err">The OLT rejected this command — syntax may differ in your firmware (adjust it in data/profiles.json).</p>'}${pre(r.text)}`;
    $("#cp-" + name).onclick = () => navigator.clipboard.writeText(r.text).then(() => toast("Copied"));
    $("#rf-" + name).onclick = () => tabText(name, path, title);
  } catch (e) { failed(el, e, () => tabText(name, path, title)); }
}
function draw() {
  const cv = $("#cv"); if (!cv) return;
  const dpr = devicePixelRatio || 1, w = cv.clientWidth * dpr, h = cv.clientHeight * dpr; cv.width = w; cv.height = h;
  const g = cv.getContext("2d"), max = Math.max(1, ...series.in, ...series.out);
  g.clearRect(0, 0, w, h); g.lineWidth = 2 * dpr;
  [["in", "#2457d6"], ["out", "#16a34a"]].forEach(([k, col]) => {
    g.strokeStyle = col; g.beginPath();
    series[k].forEach((v, i) => { const x = i / Math.max(1, 59) * w, y = h - (v / max) * (h - 10 * dpr) - 5 * dpr; i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
  });
}
async function pollTraffic() {
  try {
    const r = await api(`/api/onus/${id}/traffic`), p = r.parsed; let inb = null, outb = null;
    if (p) {
      inb = p.in_bps; outb = p.out_bps;
      if (inb == null && p.in_bytes != null && last && last.in_bytes != null) inb = Math.max(0, (p.in_bytes - last.in_bytes) * 8 / (r.ts - last.ts));
      if (outb == null && p.out_bytes != null && last && last.out_bytes != null) outb = Math.max(0, (p.out_bytes - last.out_bytes) * 8 / (r.ts - last.ts));
      last = {in_bytes: p.in_bytes, out_bytes: p.out_bytes, ts: r.ts};
    }
    series.in.push(inb || 0); series.out.push(outb || 0); series.in = series.in.slice(-60); series.out = series.out.slice(-60);
    $("#t-in").textContent = fmtBps(inb); $("#t-out").textContent = fmtBps(outb);
    $("#t-msg").innerHTML = p ? "" : '<span class="err">The parser did not recognize the output format — check the raw output and adjust the profile.</span>';
    $("#t-raw").textContent = `$ ${r.command}\n${r.raw}`; draw();
  } catch (e) { $("#t-msg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  if (!pollTraffic.lastSignal || Date.now() - pollTraffic.lastSignal >= 60000) {
    pollTraffic.lastSignal = Date.now();
    try {
      const status = await api(`/api/onus/${id}/status?summary=true`);
      updateHeaderStatus(status.status, status.phase_state, status.optical);
    } catch (e) {
      $("#signal-msg").innerHTML = `<span class="err">${esc(e.message)}</span>`;
    }
  }
}
function tabTraffic() {
  const el = $("#tab-traffic");
  if (!el.innerHTML) {
    el.innerHTML = `<div class="traffic-signal-grid">
      <div class="chart"><div class="row between"><h4>ONU traffic</h4><span class="muted small">Every 10 seconds</span></div>
        <div class="traffic-values"><span>In <b id="t-in">—</b></span><span>Out <b id="t-out">—</b></span></div>
        <canvas id="cv"></canvas><div id="t-msg"></div><details><summary>Output traffic OLT</summary><pre class="out" id="t-raw"></pre></details></div>
      <div class="chart"><div class="row between"><h4>Signal</h4><span class="muted small">Sample every 60 seconds</span></div>
        <div class="traffic-values"><span>OLT Rx <b id="signal-current">—</b></span><span>Peak <b id="signal-peak">—</b></span></div>
        <canvas id="signal-cv"></canvas><div id="signal-msg" class="muted small"></div></div>
    </div>`;
    renderSignal();
  }
  if (!timer) { pollTraffic(); timer = setInterval(pollTraffic, 10000); }
}
const MODE_KEY = {LAN: "lan", Access: "access", Hybrid: "hybrid", Trunk: "trunk", Transparent: "transparent"};
async function tabEth() {
  const el = $("#tab-eth"); loading(el);
  try {
    const r = await api(`/api/onus/${id}/eth`);
    el.innerHTML = `<h3>Ethernet ports</h3><div class="row between"><span class="muted small">Status and VLAN mode for each port. Changes are sent to the OLT after command preview and confirmation.</span><button class="sm" id="eth-rf">Refresh</button></div>
      <div class="scroll"><table><thead><tr><th>Port</th><th>Admin state</th><th>Mode</th><th>VLAN</th><th>Operate</th><th>Speed</th><th>Action</th></tr></thead><tbody>
      ${r.ports.map(p => `<tr><td>${esc(p.port)}</td><td class="${p.admin === "lock" ? "err" : ""}">${p.admin === "lock" ? "Port shutdown" : p.admin ? "Enabled" : "—"}</td>
        <td>${esc(p.mode) || "—"}</td><td>${esc(p.vlan) || "—"}</td><td>${esc(p.oper) || "—"}</td><td>${esc(p.speed) || "—"}</td>
        <td><button class="sm" data-p="${esc(p.port)}">Configure</button></td></tr>`).join("") || '<tr><td colspan="7" class="muted">Could not read ports from the OLT.</td></tr>'}</tbody></table></div>${r.ok ? "" : '<p class="err">The OLT rejected the command.</p>'}
      <details><summary>Raw OLT output</summary>${pre(r.raw)}</details>`;
    $("#eth-rf").onclick = tabEth;
    el.onclick = async e => {
      const b = e.target.closest("button[data-p]"); if (!b) return;
      const cur = r.ports.find(p => p.port === b.dataset.p) || {};
      const v = await askForm({title: `Configure ethernet port ${b.dataset.p}`, submit: "Preview commands",
        note: "DHCP 'From ONU' cannot be changed from the dashboard.", fields: [
        {k: "status", label: "Status", type: "select", value: cur.admin === "lock" ? "shutdown" : "enabled", options: [["enabled", "Enabled"], ["shutdown", "Port shutdown"]]},
        {k: "mode", label: "Mode", type: "select", value: MODE_KEY[cur.mode] || "lan", options: [["lan", "LAN"], ["access", "Access"], ["hybrid", "Hybrid"], ["trunk", "Trunk"], ["transparent", "Transparent"]]},
        {k: "vlan", label: "VLAN-ID (trunk: list, e.g. 100,200-210)", value: cur.vlan || "", when: v => ["access", "hybrid", "trunk"].includes(v.mode)}]});
      if (!v) return;
      const changed = v.mode !== (MODE_KEY[cur.mode] || "lan") || (["access", "hybrid", "trunk"].includes(v.mode) && v.vlan !== (cur.vlan || ""));
      openPlan({action: "eth_config", onu_id: id, params: {port: b.dataset.p, status: v.status, vlan: v.vlan, ...(changed ? {mode: v.mode} : {})}, onDone: () => setTimeout(tabEth, 500)});
    };
  } catch (e) { failed(el, e, tabEth); }
}
const loaders = {eth: tabEth, status: tabStatus, config: () => tabText("config", "running-config"), sw: () => tabText("sw", "sw-info"), traffic: tabTraffic};
function show(tab) {
  $$("#tabs button[data-tab]").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  if (tab !== "traffic" && timer) { clearInterval(timer); timer = null; }
  $$(".tab").forEach(s => s.hidden = s.id !== "tab-" + tab);
  if (tab === "traffic") {
    $("#tab-traffic").scrollIntoView?.({behavior: "smooth", block: "start"});
    if (!loaded[tab]) { loaded[tab] = true; loaders[tab](); }
    return;
  }
  if (tab === "status" || !loaded[tab]) { loaded[tab] = true; loaders[tab](); }
}
$("#tabs").onclick = async e => {
  const t = e.target.closest("button[data-tab]"); if (t) return show(t.dataset.tab);
  const b = e.target.closest("button[data-act]"); if (!b) return;
  if (timer) { clearInterval(timer); timer = null; }
  const act = b.dataset.act;
  if (act === "resync") {
    openPlan({action: "repush_onu", onu_id: id, onDone: () => {
      void (async () => {
        try {
          const r = await api(`/api/onus/${id}/resync`, {method: "POST"});
          toast(`ONU repush succeeded; dashboard synced from OLT (VLAN ${r.vlan ?? "—"})`);
          await header();
        } catch (err) {
          toast(`ONU repush succeeded, but dashboard sync failed: ${err.message}`, true);
        }
      })();
    }});
    return;
  }
  if (act === "reboot") {
    if (window.USER_ROLE === "olt_limited") {
      if (!confirm(`Reboot ONT ${currentOnu?.interface} on ${currentOnu?.olt.name}? The subscriber connection will be interrupted temporarily.`)) return;
      b.disabled = true;
      try {
        const result = await api(`/api/onus/${id}/reboot`, {method: "POST", body: {confirm: true}});
        toast(result.message);
        setTimeout(() => refreshHeaderStatus().catch(e => toast(`Status refresh failed: ${e.message}`, true)), 8000);
      } catch (e) { toast(e.message, true); }
      finally { b.disabled = false; }
      return;
    }
    if (currentOnu?.olt.vendor === "hioso") {
      if (!confirm(`Reboot ONT ${currentOnu.interface} on ${currentOnu.olt.name}? The subscriber connection will be interrupted temporarily.`)) return;
      b.disabled = true;
      try {
        const result = await api(`/api/onus/${id}/hioso/reboot`, {method: "POST", body: {confirm: true}});
        toast(result.message);
        setTimeout(() => refreshHeaderStatus().catch(e => toast(`Status refresh failed: ${e.message}`, true)), 8000);
      } catch (e) { toast(e.message, true); }
      finally { b.disabled = false; }
      return;
    }
    executeAction({action: "reboot_onu", onu_id: id, title: "Reboot ONU", button: b, onDone: () => header()});
    return;
  }
  if (act === "admin-toggle") {
    const values = await askForm({title: "Change ONU state", submit: "Continue", fields: [
      {k: "action", label: "Action", type: "select", value: "disable_onu",
        options: [["disable_onu", "Disable ONU"], ["enable_onu", "Enable ONU"]]}]});
    if (!values) return;
    const disabling = values.action === "disable_onu";
    const operation = disabling ? "Disable" : "Enable";
    if (!confirm(`${operation} all Ethernet ports on ONU ${currentOnu?.interface || id}?`)) return;
    executeAction({action: values.action, onu_id: id, title: `${operation} ONU`,
      button: b, onDone: () => header()});
    return;
  }
  if (act === "delete") {
    const target = currentOnu;
    if (!target) return toast("ONU details are not loaded yet. Please try again.", true);
    if (!confirm(`Are you sure you want to delete ONU ${target.sn} from ${target.olt.name} ${target.slot}/${target.pon}:${target.onu_number}? The ONU configuration will be removed from the OLT and the subscriber will be disconnected.`)) return;
    const originalLabel = b.textContent;
    b.disabled = true;
    b.textContent = "Deleting…";
    try {
      const job = await api(`/api/onus/${id}/delete`, {method: "POST", body: {confirm: true}});
      toast("ONU deletion started in the background.");
      let result = job;
      while (result.status === "queued" || result.status === "running") {
        await new Promise(resolve => setTimeout(resolve, 1000));
        result = await api(`/api/onu-deletions/${job.id}`);
      }
      if (result.status !== "completed") throw new Error(result.message || "ONU deletion failed.");
      toast(`ONU ${target.sn} deleted.`);
      location.href = "/configured";
    } catch (error) {
      toast(error.message, true);
      b.disabled = false;
      b.textContent = originalLabel;
    }
    return;
  }
  openPlan({action: act + "_onu", onu_id: id, onDone: () => header()});
};
addEventListener("beforeunload", () => {
  clearInterval(timer);
  clearInterval(window.onuWanIpTimer);
});
header().catch(e => toast(e.message, true));
