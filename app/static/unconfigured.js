let F = null, ITEMS = [];
async function init() { F = await api("/api/filters"); fillSelect($("#olt"), F.olts, "id", "name", "All OLTs"); }

async function scan() {
  const b = $("#scan"), id = $("#olt").value; b.disabled = true; b.textContent = "Scanning…";
  $("#errors").innerHTML = ""; $("#tbl tbody").innerHTML = "";
  try {
    const r = await api("/api/unconfigured" + (id ? `?olt_id=${id}` : ""));
    ITEMS = r.items;
    $("#tbl tbody").innerHTML = ITEMS.map((o, i) => `<tr><td>${esc(o.olt_name)}</td><td>${o.slot}</td><td>${o.pon}</td><td>${esc(o.location)}</td>
      <td><b>${esc(o.sn)}</b></td><td>${esc(o.state || o.model || "")}</td><td><button class="sm primary" data-i="${i}">Authorize</button></td></tr>`).join("")
      || `<tr><td colspan="7" class="muted">No ONUs awaiting registration (${r.scanned} OLTs scanned).</td></tr>`;
    $("#errors").innerHTML = r.errors.map(e => `<div class="card pad err">${esc(e.olt)}: ${esc(e.error)}</div>`).join("");
    $("#raw").textContent = Object.entries(r.raw).map(([k, v]) => `### ${k}\n${v}`).join("\n\n") || "(empty)";
  } catch (e) { toast(e.message, true); } finally { b.disabled = false; b.textContent = "Scan"; }
}
$("#scan").onclick = scan;

async function openRegister(it) {
  const [vl, pon, ot] = await Promise.all([api(`/api/olts/${it.olt_id}/vlans`), api(`/api/olts/${it.olt_id}/pon-ports`),
    api(`/api/olts/${it.olt_id}/onu-types`)]);
  const port = pon.slots.flatMap(s => s.ports).find(p => p.slot === it.slot && p.port === it.pon);
  const used = port ? port.total : 0, cap = port ? port.capacity : 128;
  const sp = (d) => F.profiles.filter(p => p.direction === d);
  const opt = (arr, lab = x => x.name) => '<option value="">—</option>' + arr.map(x => `<option value="${x.id}">${esc(lab(x))}</option>`).join("");
  // VLAN choices come from this OLT's database (VLAN 1 is the default and is not offered). ONU types come from this OLT and Settings.
  const vlans = vl.rows.filter(v => v.vlan > 1);
  const vlanOpts = vlans.length ? '<option value="">— select VLAN —</option>' + vlans.map(v => `<option value="${v.vlan}">${v.vlan}${v.name ? " — " + esc(v.name) : ""}</option>`).join("")
    : '<option value="">(no VLANs on this OLT — add them in Settings → OLTs → VLANs)</option>';
  const tOpt = t => `<option value="${esc(t.name)}">${esc(t.name)}${t.eth_ports != null ? ` (${t.eth_ports} eth)` : ""}</option>`;
  const typeOpts = rows => {
    const onOlt = rows.filter(t => t.on_olt), other = rows.filter(t => !t.on_olt);
    return rows.length ? '<option value="">— select ONU type —</option>' +
      (onOlt.length ? `<optgroup label="Available on this OLT">${onOlt.map(tOpt).join("")}</optgroup>` : "") +
      (other.length ? `<optgroup label="Settings → ONU types (may not exist on OLT)">${other.map(tOpt).join("")}</optgroup>` : "")
      : '<option value="">(no ONU types — click ↻ OLT or add one in Settings → ONU types)</option>';
  };
  const form = $("#reg-form");
  form.innerHTML = `<h3>Authorize ONU ${esc(it.sn)}</h3>
    <div class="muted small">OLT <b>${esc(it.olt_name)}</b> · board ${it.slot} · port ${it.pon} · used <span class="usage"><i style="width:${used / cap * 100}%"></i></span> ${used}/${cap} (${cap - used} available)</div>
    <div class="grid2">
      <label>ONU type<span class="row gap"><select name="onu_type" required style="flex:1;min-width:0">${typeOpts(ot.rows)}</select><button type="button" class="sm" id="rg-types" title="Reload ONU types from the OLT (show onu-type gpon)">↻ OLT</button></span></label>
      <label>ONU mode<select name="mode"><option value="bridge">Bridging</option><option value="routing">Routing (PPPoE)</option></select></label>
      <label class="pp" hidden>PPPoE username<input name="pppoe_user"></label><label class="pp" hidden>PPPoE password<input name="pppoe_password" type="password" autocomplete="new-password"></label>
      <label>User VLAN-ID<select name="vlan" required>${vlanOpts}</select></label>
      <label>Service name<input name="service" value="INT" required></label>
      <label>Zone<select name="zone_id">${opt(F.zones)}</select></label><label>ODB (Splitter)<select name="odb_id">${opt(F.odbs)}</select></label>
      <label>Download speed<select name="dl_profile_id" required>${opt(sp("download"))}</select></label><label>Upload speed<select name="ul_profile_id" required>${opt(sp("upload"))}</select></label>
      <label>Name<input name="name" maxlength="64" required></label><label>ONU external ID<input name="external_id" value="${esc(it.sn)}" maxlength="64"></label>
      <label class="wide">Address or comment<input name="address" maxlength="300"></label><label>Contact<input name="contact" maxlength="100"></label>
    </div>
    <div class="row end"><button type="button" id="rg-cancel">Cancel</button><button type="submit" class="primary">OK</button></div>`;
  const dlg = $("#reg-dialog"), mode = form.elements.mode;
  if (it.model && [...form.elements.onu_type.options].some(o => o.value === it.model)) form.elements.onu_type.value = it.model;   // C6xx reports the ONU model
  mode.onchange = () => $$(".pp", form).forEach(l => l.hidden = mode.value !== "routing");
  $("#rg-cancel").onclick = () => dlg.close();
  $("#rg-types").onclick = async ev => {
    const b = ev.target, sel = form.elements.onu_type, cur = sel.value; b.disabled = true;
    try {
      const r = await api(`/api/olts/${it.olt_id}/onu-types?live=1`);
      sel.innerHTML = typeOpts(r.rows); if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
      toast(r.live_msg || "ONU type list updated", !/read/.test(r.live_msg || "read"));
    } catch (e) { toast(e.message, true); } finally { b.disabled = false; }
  };
  form.onsubmit = async e => {
    e.preventDefault();
    const v = {}; [...form.elements].forEach(el => { if (el.name) v[el.name] = el.value; });
    ["zone_id", "odb_id", "dl_profile_id", "ul_profile_id", "vlan"].forEach(k => v[k] = v[k] ? +v[k] : null);
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true; submit.textContent = "Processing…";
    const ok = await executeAction({action: "register_onu", olt_id: it.olt_id,
      params: {...v, slot: it.slot, pon: it.pon, sn: it.sn}, title: "ONU registration", onDone: () => scan()});
    if (ok) dlg.close();
    else { submit.disabled = false; submit.textContent = "OK"; }
  };
  dlg.showModal();
}
$("#tbl").onclick = e => { const b = e.target.closest("button[data-i]"); if (b) openRegister(ITEMS[+b.dataset.i]).catch(err => toast(err.message, true)); };
init().catch(e => toast(e.message, true));
