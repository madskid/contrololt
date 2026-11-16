(async () => {
  const rows = await api("/api/audit?limit=200");
  $("#tbl tbody").innerHTML = rows.map((a, i) => `<tr><td>${new Date(a.ts).toLocaleString("en-US")}</td><td>${esc(a.user)}</td><td>${esc(a.olt)}</td>
    <td>${esc(a.action)}</td><td>${esc(a.target)}</td><td class="${a.ok ? "" : "err"}">${a.ok ? "OK" : "Failed"}</td>
    <td><details><summary>Details</summary><pre class="out">${esc(a.commands)}\n\n--- output ---\n${esc(a.output)}</pre></details></td></tr>`).join("")
    || '<tr><td colspan="7" class="muted">No changes have been sent to an OLT yet.</td></tr>';
})().catch(e => toast(e.message, true));
