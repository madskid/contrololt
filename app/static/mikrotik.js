let ROUTERS = new Map(), editingId = null;
const dlg = $("#router-dialog"), form = $("#router-form");

function row(r) {
  const chk = r.last_check_at ? ago(r.last_check_at) : "belum pernah";
  return `<tr data-id="${r.id}">
    <td>${r.id}</td>
    <td>${badge(r.status)}</td>
    <td><a href="/mikrotik/${r.id}"><b>${esc(r.name)}</b></a></td>
    <td>${esc(r.ip)}</td>
    <td>${r.api_port} ${r.use_ssl ? '<span class="badge" style="background:#0b5fc4">SSL</span>' : ''}</td>
    <td>${esc(r.identity || "—")}</td>
    <td>${esc(r.board || "—")}</td>
    <td>${esc(r.ros_version || "—")}</td>
    <td>${esc(r.uptime || "—")}</td>
    <td>${chk}</td>
    <td class="row">
      <a class="btn" href="/mikrotik/${r.id}">Manage</a>
      <button class="sm" data-act="check">Check</button>
      <button class="sm" data-act="edit">Edit</button>
      <button class="sm danger" data-act="del">Delete</button>
    </td>
  </tr>`;
}

function render() {
  const q = ($("#f-q").value || "").toLowerCase(), st = $("#f-status").value;
  const list = [...ROUTERS.values()].filter(r =>
    (!st || r.status === st) &&
    (!q || [r.name, r.ip, r.identity, r.board].some(x => (x || "").toLowerCase().includes(q)))
  );
  $("#router-table tbody").innerHTML = list.map(row).join("");
  $("#router-empty").hidden = ROUTERS.size > 0;
}

async function load() {
  const list = await api("/api/mikrotik/routers");
  ROUTERS = new Map(list.map(r => [r.id, r]));
  render();
}

function payload() {
  const f = form.elements;
  return {
    name: f.name.value.trim(),
    ip: f.ip.value.trim(),
    api_port: +f.api_port.value,
    api_user: f.api_user.value.trim(),
    api_pass: f.api_pass.value,
    use_ssl: f.use_ssl.checked || +f.api_port.value === 8729,
  };
}

function openDialog(r) {
  editingId = r ? r.id : null;
  form.reset();
  $("#test-out").hidden = true;
  $("#dlg-title").textContent = r ? "Edit MikroTik Router" : "Add MikroTik Router";
  $("#pw-hint").hidden = !r;
  form.elements.api_pass.required = !r;

  if (r) {
    form.elements.name.value = r.name;
    form.elements.ip.value = r.ip;
    form.elements.api_port.value = r.api_port;
    form.elements.api_user.value = r.api_user;
    form.elements.use_ssl.checked = !!r.use_ssl;
  } else {
    form.elements.api_port.value = 8728;
    form.elements.api_user.value = "admin";
  }
  dlg.showModal();
}

const btnAdd = $("#btn-add-router");
if (btnAdd) btnAdd.onclick = () => openDialog(null);

form.elements.api_port.oninput = () => {
  if (+form.elements.api_port.value === 8729) form.elements.use_ssl.checked = true;
  if (+form.elements.api_port.value === 8728) form.elements.use_ssl.checked = false;
};

$("#btn-cancel").onclick = () => dlg.close();

$("#btn-test").onclick = async () => {
  const out = $("#test-out");
  out.hidden = false;
  out.textContent = "Testing koneksi ke RouterOS native API…";
  try {
    const p = payload();
    if (editingId && !p.api_pass) {
      throw new Error("Masukkan password untuk melakukan test koneksi");
    }
    const r = await api("/api/mikrotik/routers/test", {method: "POST", body: p});
    if (r.ok) {
      out.textContent = `Koneksi BERHASIL!\nIdentity: ${r.identity}\nRouterOS: ${r.version}\nBoard   : ${r.board}\nUptime  : ${r.uptime}`;
    } else {
      out.textContent = `Koneksi GAGAL!\nDetail: ${r.detail}`;
    }
  } catch (e) {
    out.textContent = "Error: " + e.message;
  }
};

form.onsubmit = async ev => {
  ev.preventDefault();
  const btn = $("#btn-save");
  btn.disabled = true;
  try {
    const p = payload();
    if (editingId && !p.api_pass) delete p.api_pass;
    const r = await api(
      editingId ? `/api/mikrotik/routers/${editingId}` : "/api/mikrotik/routers",
      {method: editingId ? "PUT" : "POST", body: p}
    );
    ROUTERS.set(r.id, r);
    render();
    dlg.close();
    toast(editingId ? "Router berhasil diperbarui" : "Router berhasil ditambahkan");
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

$("#router-table").addEventListener("click", async ev => {
  const b = ev.target.closest("button[data-act]");
  if (!b) return;
  const id = +b.closest("tr").dataset.id, r = ROUTERS.get(id);
  try {
    if (b.dataset.act === "edit") {
      openDialog(r);
    } else if (b.dataset.act === "check") {
      b.disabled = true;
      const res = await api(`/api/mikrotik/routers/${id}/check`, {method: "POST"});
      if (res.ok) {
        ROUTERS.set(id, res);
        render();
        toast(`Router ${r.name} online (${res.identity})`);
      } else {
        toast(`Router offline: ${res.detail}`, true);
      }
      b.disabled = false;
    } else if (b.dataset.act === "del") {
      if (confirm(`Hapus router MikroTik "${r.name}" (${r.ip})?`)) {
        await api(`/api/mikrotik/routers/${id}`, {method: "DELETE"});
        ROUTERS.delete(id);
        render();
        toast("Router berhasil dihapus");
      }
    }
  } catch (e) {
    toast(e.message, true);
    b.disabled = false;
  }
});

load().catch(e => toast(e.message, true));
$("#f-q").oninput = render;
$("#f-status").onchange = render;
