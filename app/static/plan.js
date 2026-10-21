// Preview -> confirmation -> execute OLT changes. Used across all pages.
async function openPlan({action, olt_id, onu_id, params = {}, onDone}) {
  const dlg = $("#plan-dialog");
  let plan;
  try { plan = await api("/api/plan", {method: "POST", body: {action, olt_id, onu_id, params}}); }
  catch (e) { toast(e.message, true); if (dlg.open) dlg.close(); return; }
  const can = plan.olt.write_enabled;
  dlg.innerHTML = `<div class="stack"><h3>${esc(plan.title)}</h3>
    <div class="muted small">OLT <b>${esc(plan.olt.name)}</b> · target <code>${esc(plan.target)}</code></div>
    ${plan.dangerous ? '<p class="err"><b>Risky action.</b> Review the commands carefully.</p>' : ""}
    ${plan.warnings.map(w => `<p class="small" style="color:var(--warn)">⚠ ${esc(w)}</p>`).join("")}
    <label>Commands to send (editable; the server only accepts allowed commands)
      <textarea id="pl-cmds" rows="${Math.min(18, plan.commands.length + 1)}" spellcheck="false" style="font:12px ui-monospace,monospace">${esc(plan.commands.join("\n"))}</textarea></label>
    ${plan.rollback.length ? `<details><summary>Automatic rollback if the OLT rejects a command</summary><pre class="out">${esc(plan.rollback.join("\n"))}</pre></details>` : ""}
    ${can ? "" : '<p class="err">This OLT is not enabled for changes. Enable "Allow changes to OLT" in Settings → OLTs → Edit.</p>'}
    <label style="flex-direction:row;gap:.5rem;align-items:center;font-size:13px;color:var(--fg)"><input type="checkbox" id="pl-save"> Save the OLT config after success (the <code>write</code> command)</label>
    <label style="flex-direction:row;gap:.5rem;align-items:center;font-size:13px;color:var(--fg)"><input type="checkbox" id="pl-ok"> I have reviewed the commands above</label>
    <div id="pl-result"></div>
    <div class="row end"><button type="button" id="pl-cancel">Close</button><button type="button" class="primary" id="pl-run" disabled>Execute</button></div></div>`;
  const run = $("#pl-run"), chk = $("#pl-ok");
  chk.onchange = () => run.disabled = !(chk.checked && can);
  $("#pl-cancel").onclick = () => dlg.close();
  run.onclick = async () => {
    run.disabled = true; chk.disabled = true; $("#pl-result").innerHTML = '<p class="muted">Sending to OLT…</p>';
    const lines = $("#pl-cmds").value.split("\n").map(x => x.trim()).filter(Boolean);
    const edited = JSON.stringify(lines) !== JSON.stringify(plan.commands);
    try {
      const r = await api("/api/exec", {method: "POST", body: {action, olt_id, onu_id, params, confirm: true, save_after: $("#pl-save").checked, ...(edited ? {commands: lines} : {})}});
      $("#pl-result").innerHTML = `<p class="${r.ok ? "" : "err"}"><b>${r.ok ? "Success" : "Failed"}:</b> ${esc(r.message)}${r.rolled_back ? " — rollback executed" : ""}
        ${r.saved ? ` · config save: ${r.saved.ok ? "OK" : "failed (" + esc(r.saved.message) + ")"}` : ""}</p>
        <pre class="out">${esc(r.log.map(x => `${x.rollback ? "[rollback] " : ""}${x.error ? "✗" : "✓"} ${x.cmd}${x.fallback_to ? "\n    rejected; trying alternative: " + x.fallback_to : ""}${x.output ? "\n    " + x.output.replace(/\n/g, "\n    ") : ""}`).join("\n"))}</pre>`;
      if (r.ok) { toast(plan.title + ": succeeded"); if (onDone) onDone(r); }
    } catch (e) { $("#pl-result").innerHTML = `<p class="err">${esc(e.message)}</p>`; chk.disabled = false; }
  };
  if (!dlg.open) dlg.showModal();
}

// Execute a form action directly after user confirmation, without showing a command preview.
async function executeAction({action, olt_id, onu_id, params = {}, title, button, onDone}) {
  if (button?.disabled) return false;
  if (button) button.disabled = true;
  toast(`${title}: processing…`);
  let result;
  try {
    result = await api("/api/exec", {method: "POST", body: {action, olt_id, onu_id, params, confirm: true}});
  } catch (e) {
    toast(`${title}: failed — ${e.message}`, true);
    if (button) button.disabled = false;
    return false;
  }
  if (!result.ok) {
    toast(`${title}: failed — ${result.message}`, true);
    if (button) button.disabled = false;
    return false;
  }
  toast(`${title}: succeeded`);
  if (onDone) {
    try { await onDone(result); }
    catch (e) { toast(`${title} succeeded, but the page could not be refreshed: ${e.message}`, true); }
  }
  if (button) button.disabled = false;
  return true;
}

// Reusable form in the same dialog. Types: text (default) | password | select (options [v, label, disabled]) | checks.
// field.when(values) -> show/hide; field.note -> help text. Resolves values or null when canceled.
function askForm({title, fields, submit = "Lanjut", note = ""}) {
  const dlg = $("#plan-dialog");
  const input = f => f.type === "select"
    ? `<select name="${f.k}">${f.options.map(([v, l, dis]) => `<option value="${esc(v)}" ${dis ? "disabled" : ""} ${String(v) === String(f.value ?? "") ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`
    : f.type === "checks"
      ? `<span class="row wrap gap">${f.options.map(([v, l]) => `<label style="flex-direction:row;gap:.3rem;align-items:center;color:var(--fg)"><input type="checkbox" data-checks="${f.k}" value="${esc(v)}"> ${esc(l)}</label>`).join("") || '<span class="muted small">(none)</span>'}</span>`
      : `<input name="${f.k}" type="${f.type === "password" ? "password" : "text"}" value="${esc(f.value ?? "")}" placeholder="${esc(f.placeholder || "")}" ${f.type === "password" ? 'autocomplete="new-password"' : ""} ${f.req === false ? "" : "required"}>`;
  return new Promise(res => {
    dlg.innerHTML = `<form class="stack" method="dialog"><h3>${esc(title)}</h3>${note ? `<div class="muted small">${esc(note)}</div>` : ""}
      <div class="grid2">${fields.map(f => `<label data-f="${f.k}">${esc(f.label)}${input(f)}${f.note ? `<span class="muted small">${esc(f.note)}</span>` : ""}</label>`).join("")}</div>
      <div class="row end"><button type="button" id="af-cancel">Cancel</button><button class="primary">${esc(submit)}</button></div></form>`;
    const form = dlg.querySelector("form");
    const vals = () => { const v = {}; fields.forEach(f => { v[f.k] = f.type === "checks" ? $$(`[data-checks="${f.k}"]:checked`, form).map(x => x.value).join(",") : form.elements[f.k].value; }); return v; };
    const refresh = () => { const v = vals(); fields.forEach(f => { if (!f.when) return; const lab = $(`[data-f="${f.k}"]`, form), show = !!f.when(v);
      lab.hidden = !show; const el = form.elements[f.k]; if (el && f.req !== false && f.type !== "checks") el.required = show; }); };
    form.addEventListener("input", refresh); form.addEventListener("change", refresh); refresh();
    $("#af-cancel").onclick = () => { dlg.close(); res(null); };
    form.onsubmit = e => { e.preventDefault(); res(vals()); };
    if (!dlg.open) dlg.showModal();
  });
}
