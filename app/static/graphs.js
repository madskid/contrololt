// OLT-level graphs: uplink/PON traffic (IF-MIB), average signal per PON, and ONU status. Sampled every 5 minutes.
const COLORS = ["#2457d6", "#16a34a", "#d97706", "#dc2626", "#7c3aed", "#0891b2"];
const Q = new URLSearchParams(location.search);
let tab = Q.get("tab") || "olt";
const natural = (a, b) => a.localeCompare(b, undefined, {numeric: true});
const fmtVal = (v, unit) => unit === "bps" ? (v >= 1e9 ? (v / 1e9).toFixed(2) + "G" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(0) + "k" : Math.round(v) + "")
  : unit === "dBm" || unit === "c" ? v.toFixed(1) : Math.round(v) + "";

function lineChart(cv, series, unit) {
  const dpr = devicePixelRatio || 1, w = cv.clientWidth * dpr, h = cv.clientHeight * dpr; cv.width = w; cv.height = h;
  const g = cv.getContext("2d"), fg = getComputedStyle(document.body).color;
  const all = series.flatMap(s => s.pts.map(p => [new Date(p[0]).getTime(), p[1]]));
  g.clearRect(0, 0, w, h); g.font = `${11 * dpr}px sans-serif`; g.fillStyle = fg; g.strokeStyle = fg + "33";
  if (!all.length) { g.fillText("No data yet (sampled every 5 minutes)", 10 * dpr, 24 * dpr); return; }
  let t0 = Math.min(...all.map(a => a[0])), t1 = Math.max(...all.map(a => a[0])); if (t1 === t0) t1 = t0 + 1;
  let v0 = Math.min(...all.map(a => a[1])), v1 = Math.max(...all.map(a => a[1]));
  if (unit === "bps" || unit === "count") v0 = 0; if (v1 === v0) v1 = v0 + 1;
  const L = 46 * dpr, R = 8 * dpr, T = 8 * dpr, B = 18 * dpr, X = t => L + (t - t0) / (t1 - t0) * (w - L - R), Y = v => h - B - (v - v0) / (v1 - v0) * (h - T - B);
  for (let i = 0; i <= 4; i++) { const v = v0 + (v1 - v0) * i / 4, y = Y(v); g.beginPath(); g.moveTo(L, y); g.lineTo(w - R, y); g.stroke(); g.fillText(fmtVal(v, unit), 2 * dpr, y + 4 * dpr); }
  const fmtT = t => new Date(t).toLocaleString("en-US", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false});
  g.fillText(fmtT(t0), L, h - 3 * dpr); const e = fmtT(t1); g.fillText(e, w - R - g.measureText(e).width, h - 3 * dpr);
  series.forEach((s, i) => { if (!s.pts.length) return; g.strokeStyle = s.color || COLORS[i % COLORS.length]; g.lineWidth = 1.6 * dpr; g.beginPath();
    s.pts.forEach((p, j) => { const x = X(new Date(p[0]).getTime()), y = Y(p[1]); j ? g.lineTo(x, y) : g.moveTo(x, y); }); g.stroke(); });
}
function addChart(title, series, unit) {
  const d = document.createElement("div"); d.className = "chart";
  d.innerHTML = `<h4>${esc(title)}</h4><canvas></canvas><div class="legend">${series.map((s, i) => `<span style="color:${s.color || COLORS[i % COLORS.length]}">● ${esc(s.name)}</span>`).join("")}</div>`;
  $("#grid").appendChild(d); lineChart($("canvas", d), series, unit);
}
const get = async metric => (await api(`/api/graphs?olt_id=${$("#olt").value}&metric=${metric}&hours=${$("#hours").value}`)).series;
const isPon = n => /^gpon[-_]olt/i.test(n);
const portNum = n => (String(n).match(/\d+/g) || []).join("/");     // gpon-olt_1/2/3 (C300) dan gpon_olt-1/2/3 (C600) -> "1/2/3"

async function render() {
  const grid = $("#grid"); grid.innerHTML = "";
  if (!$("#olt").value) { grid.innerHTML = '<div class="card pad muted">No OLTs available.</div>'; return; }
  try {
    if (tab === "olt") {
      const [rx, tx, tmp] = await Promise.all([get("up_rx"), get("up_tx"), get("up_temp")]);
      [...new Set([...Object.keys(rx), ...Object.keys(tx)])].sort(natural).forEach(n => addChart(`${n} — module optical power (dBm)`, [{name: "Rx", pts: rx[n] || []}, {name: "Tx", pts: tx[n] || []}], "dBm"));
      Object.keys(tmp).sort(natural).forEach(n => addChart(`${n} — optical module temperature (°C)`, [{name: "Temperature", pts: tmp[n]}], "c"));
      const card = document.createElement("div"); card.className = "card pad"; card.style.gridColumn = "1/-1";
      card.innerHTML = `<div class="row gap"><button id="g-diag">Check OLT health now</button><span class="muted small">OLT temperature/fan/CPU/memory: ZTE output format is unverified, so raw output is shown instead of a chart.</span></div><div id="g-diag-out"></div>` +
        (Object.keys(rx).length ? "" : '<p class="muted">No uplink optical data yet (the first sample appears about 5–10 minutes after adding the OLT).</p>');
      grid.appendChild(card);
      $("#g-diag").onclick = async ev => {
        ev.target.disabled = true; $("#g-diag-out").innerHTML = '<p class="muted">Running diagnostic commands…</p>';
        try { const r = await api(`/api/olts/${$("#olt").value}/diag`);
          $("#g-diag-out").innerHTML = r.rows.map(x => `<details><summary>${esc(x.command)}${x.ok ? "" : ' <span class="err">(rejected by OLT)</span>'}</summary><pre class="out">${esc(x.output)}</pre></details>`).join(""); }
        catch (e) { $("#g-diag-out").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
        ev.target.disabled = false; };
    } else if (tab === "uplink" || tab === "pon") {
      const [i, o] = await Promise.all([get("if_in"), get("if_out")]);
      const want = Q.get("key"), names = [...new Set([...Object.keys(i), ...Object.keys(o)])].filter(n => isPon(n) === (tab === "pon") && (!want || portNum(n) === portNum(want))).sort(natural);
      names.forEach(n => addChart(n, [{name: "In", pts: i[n] || []}, {name: "Out", pts: o[n] || []}], "bps"));
      if (!names.length) grid.innerHTML = '<div class="card pad muted">No data yet. Make sure SNMP is enabled; the first sample appears after about 5–10 minutes.</div>';
    } else if (tab === "traffic") {
      const [i, o] = await Promise.all([get("if_in"), get("if_out")]);
      const sum = ser => { const m = {}; Object.entries(ser).filter(([n]) => !isPon(n)).forEach(([, pts]) => pts.forEach(([t, v]) => m[t] = (m[t] || 0) + v));
        return Object.entries(m).sort().map(([t, v]) => [t, v]); };
      addChart("Total traffic uplink", [{name: "In", pts: sum(i)}, {name: "Out", pts: sum(o)}], "bps");
    } else if (tab === "signal") {
      const s = await get("pon_rx");
      Object.keys(s).sort(natural).forEach(k => addChart(`PON ${k} — average ONU Rx (dBm)`, [{name: "Rx avg", pts: s[k]}], "dBm"));
      if (!Object.keys(s).length) grid.innerHTML = '<div class="card pad muted">No data yet.</div>';
    } else if (tab === "status") {
      const s = await get("onu_status"), col = {online: "#16a34a", offline: "#8b93a5", los: "#dc2626", power_fail: "#d97706"};
      addChart("ONU count by status", Object.keys(s).map(k => ({name: LABEL[k] || k, color: col[k], pts: s[k]})), "count");
    }
  } catch (e) { grid.innerHTML = `<div class="card pad err">${esc(e.message)}</div>`; }
}
$("#tabs").onclick = e => { const b = e.target.closest("button[data-t]"); if (!b) return; tab = b.dataset.t; Q.delete("key");
  $$("#tabs button").forEach(x => x.classList.toggle("on", x === b)); render(); };
$("#olt").onchange = () => { Q.delete("key"); render(); }; $("#hours").onchange = render;
addEventListener("resize", debounce(render, 400));
(async () => {
  const f = await api("/api/filters"); $("#olt").innerHTML = f.olts.map(o => `<option value="${o.id}">${esc(o.name)}</option>`).join("");
  if (Q.get("olt")) $("#olt").value = Q.get("olt");
  $$("#tabs button").forEach(x => x.classList.toggle("on", x.dataset.t === tab)); render();
})().catch(e => toast(e.message, true));
