let ROUTERS = [], SELECTED_ROUTER = null, SECRETS = [], ACTIVE = [], PROFILES = [];
let editingId = null, profileId = null;
let statusFilter = "all";

const routerSelect = $("#pppoe-router");
const secretDialog = $("#secret-dialog"), secretForm = $("#secret-form");
const editDialog = $("#secret-edit-dialog"), editForm = $("#secret-edit-form");
const profileDialog = $("#change-profile-dialog"), profileForm = $("#change-profile-form");
const routerKey = "contrololt.selectedMikroRouter";
let commentEdited = false;

function selectedRouterId() {
  return Number(routerSelect.value);
}

function profileOptions() {
  const opts = PROFILES.map(p =>
    `<option value="${esc(p.name)}">${esc(p.name)}${p.rate_limit ? ` (${esc(p.rate_limit)})` : ""}</option>`
  ).join("");
  $$("select[name='profile']").forEach(el => { el.innerHTML = opts; });
  $("#cp-select").innerHTML = opts;
}

function activeByName() {
  return new Map(ACTIVE.map(a => [a.name, a]));
}

function render() {
  const active = activeByName();
  const query = ($("#pppoe-search").value || "").trim().toLowerCase();
  const filtered = SECRETS.filter(secret => {
    const session = active.get(secret.name);
    const category = secret.disabled ? "disabled" : session ? "online" : "offline";
    const matchesStatus = statusFilter === "all" || category === statusFilter;
    const matchesQuery = !query || [
      secret.name, secret.profile, secret.comment, session?.address, session?.caller_id,
      secret.caller_id, session?.onu?.name, session?.onu?.sn,
      secret.last_onu?.name, secret.last_onu?.sn,
    ].some(value => (value || "").toLowerCase().includes(query));
    return matchesStatus && matchesQuery;
  });
  const online = SECRETS.filter(s => !s.disabled && active.has(s.name)).length;
  const disabled = SECRETS.filter(s => s.disabled).length;
  $("#stat-total").textContent = SECRETS.length.toLocaleString();
  $("#stat-online").textContent = online.toLocaleString();
  $("#stat-offline").textContent = Math.max(0, SECRETS.length - online - disabled).toLocaleString();
  $("#stat-disabled").textContent = disabled.toLocaleString();
  $$(".pppoe-stat-filter").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.statusFilter === statusFilter));
  });

  const isAdmin = window.USER_ROLE !== "read_only";
  $("#pppoe-table tbody").innerHTML = filtered.map(s => {
    const session = active.get(s.name);
    const status = s.disabled
      ? '<span class="badge st-offline">Disabled</span>'
      : session ? '<span class="badge" style="background:#d6f0df;color:#176b3a">● Online</span>'
        : '<span class="badge st-pending">Offline</span>';
    const lastOnu = session?.onu || s.last_onu;
    const onuLabel = lastOnu
      ? `<small class="muted" title="${esc(lastOnu.sn || "")}">ONU: ${esc(lastOnu.name || lastOnu.sn || "—")}${lastOnu.name && lastOnu.sn ? ` · ${esc(lastOnu.sn)}` : ""}</small>`
      : "";
    return `<tr data-id="${esc(s.id)}" data-name="${esc(s.name)}" data-profile="${esc(s.profile)}" data-comment="${esc(s.comment)}" data-disabled="${s.disabled ? "1" : "0"}" data-active-id="${esc(session?.id || "")}">
      <td><b>${esc(s.name)}</b></td>
      <td><span class="badge" style="background:#6c757d">${esc(s.profile || "—")}</span></td>
      <td>${status}</td>
      <td><code>${esc(session?.address || "—")}</code></td>
      <td>${esc(session?.uptime || "—")}</td>
      <td><code class="pppoe-online">${esc(session?.caller_id || s.caller_id || "—")}</code>${onuLabel ? `<br>${onuLabel}` : ""}</td>
      <td>${esc(s.comment || "—")}</td>
      <td><div class="pppoe-actions">
        <button class="sm" data-act="lookup" title="Cek MAC dan ONU OLT" aria-label="Cek MAC dan ONU OLT">⌕</button>
        ${isAdmin ? `<button class="sm" data-act="copy-credentials" title="Salin username dan password" aria-label="Salin username dan password">▢</button>
        <button class="sm" data-act="edit" title="Edit user" aria-label="Edit user">✎</button>
        <button class="sm" data-act="profile" title="Ganti profile" aria-label="Ganti profile">⇄</button>
        ${session ? `<button class="sm" data-act="disconnect" title="Putus sesi agar redial" aria-label="Putus sesi">Ⅱ</button>` : ""}
        <button class="sm danger" data-act="delete" title="Hapus user" aria-label="Hapus user">▤</button>` : ""}
      </div></td>
    </tr>`;
  }).join("");
  $("#pppoe-empty").hidden = filtered.length > 0;
  $("#pppoe-empty").textContent = SECRETS.length
    ? "Tidak ada user yang cocok dengan filter atau pencarian."
    : "Tidak ada PPPoE user di router ini.";
}

$$(".pppoe-stat-filter").forEach(button => {
  button.addEventListener("click", () => {
    const selectedFilter = button.getAttribute("data-status-filter");
    statusFilter = selectedFilter === statusFilter && selectedFilter !== "all" ? "all" : selectedFilter;
    render();
  });
});

async function loadData() {
  if (!SELECTED_ROUTER) return;
  const rid = SELECTED_ROUTER.id;
  const [secrets, active, profiles] = await Promise.all([
    api(`/api/mikrotik/routers/${rid}/secrets`),
    api(`/api/mikrotik/routers/${rid}/active`),
    api(`/api/mikrotik/routers/${rid}/profiles`),
  ]);
  SECRETS = secrets;
  ACTIVE = active;
  PROFILES = profiles;
  profileOptions();
  render();
}

async function loadRouters() {
  ROUTERS = await api("/api/mikrotik/routers");
  const previous = Number(sessionStorage.getItem(routerKey) || routerSelect.value);
  routerSelect.innerHTML = ROUTERS.length
    ? ROUTERS.map(r => `<option value="${r.id}">${esc(r.name)} [${esc(r.ip)}:${r.api_port}]</option>`).join("")
    : '<option value="">Belum ada router MikroTik</option>';
  if (!ROUTERS.length) {
    SELECTED_ROUTER = null;
    SECRETS = ACTIVE = PROFILES = [];
    render();
    $("#pppoe-empty").hidden = false;
    $("#pppoe-empty").textContent = "Tambahkan router MikroTik melalui Settings → MikroTik Routers.";
    const addButton = $("#btn-add-secret");
    if (addButton) addButton.disabled = true;
    return;
  }
  const selected = ROUTERS.find(r => r.id === previous) || ROUTERS[0];
  routerSelect.value = selected.id;
  SELECTED_ROUTER = selected;
  sessionStorage.setItem(routerKey, selected.id);
  const addButton = $("#btn-add-secret");
  if (addButton) addButton.disabled = false;
  await loadData();
}

function routerId() {
  if (!SELECTED_ROUTER) throw new Error("Pilih router MikroTik terlebih dahulu.");
  return SELECTED_ROUTER.id;
}

function setBusy(button, busy) {
  if (button) button.disabled = busy;
}

function openEdit(row) {
  editingId = row.dataset.id;
  editForm.reset();
  profileOptions();
  $("#edit-username").textContent = row.dataset.name;
  editForm.elements.profile.value = row.dataset.profile;
  editForm.elements.comment.value = row.dataset.comment;
  editForm.elements.disabled.checked = row.dataset.disabled === "1";
  editDialog.showModal();
}

$("#pppoe-table").addEventListener("click", async event => {
  const button = event.target.closest("button[data-act]");
  if (!button) return;
  const row = button.closest("tr");
  const action = button.dataset.act;
  const rid = routerId();
  const sid = encodeURIComponent(row.dataset.id);
  try {
    if (action === "edit") {
      openEdit(row);
    } else if (action === "copy-credentials") {
      setBusy(button, true);
      const credentials = await api(
        `/api/mikrotik/routers/${rid}/secrets/${sid}/credentials`,
      );
      await copyText(
        `Username: ${credentials.username}\nPassword: ${credentials.password}`,
        "Username dan password",
      );
    } else if (action === "profile") {
      profileId = row.dataset.id;
      $("#cp-username").textContent = row.dataset.name;
      profileOptions();
      $("#cp-select").value = row.dataset.profile;
      profileDialog.showModal();
    } else if (action === "delete") {
      if (!confirm(`Hapus PPPoE user "${row.dataset.name}" dari router?`)) return;
      setBusy(button, true);
      await api(`/api/mikrotik/routers/${rid}/secrets/${sid}`, {method: "DELETE"});
      toast(`User ${row.dataset.name} berhasil dihapus`);
      await loadData();
    } else if (action === "disconnect") {
      if (!confirm(`Putus sesi "${row.dataset.name}" agar perangkat melakukan redial?`)) return;
      setBusy(button, true);
      await api(`/api/mikrotik/routers/${rid}/active/${encodeURIComponent(row.dataset.activeId)}`, {method: "DELETE"});
      toast(`Sesi ${row.dataset.name} diputus`);
      await loadData();
    } else if (action === "lookup") {
      await lookupOnu(row.dataset.name);
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
});

$("#btn-add-secret").onclick = () => {
  secretForm.reset();
  commentEdited = false;
  profileOptions();
  updateGeneratedComment();
  secretDialog.showModal();
};

function updateGeneratedComment() {
  if (commentEdited) return;
  const profile = secretForm.elements.profile.value;
  if (!profile) {
    secretForm.elements.comment.value = "";
    return;
  }
  const date = new Intl.DateTimeFormat("en-GB", {
    day: "2-digit", month: "short", year: "numeric",
  }).format(new Date()).replace(/\s/g, "-");
  secretForm.elements.comment.value = `activation ${date} ${profile}`;
}

function passwordFromUsername(username) {
  return username.trim().split(/[_-]/, 1)[0]
    .replace(/[^a-zA-Z0-9]/g, "")
    .replace(/[aeios]/gi, character => ({
      a: "4",
      e: "3",
      i: "1",
      o: "0",
      s: "5",
    })[character.toLowerCase()]);
}

function updatePasswordFromUsername() {
  secretForm.elements.password.value = passwordFromUsername(secretForm.elements.name.value);
}

function randomPassword() {
  const prefix = passwordFromUsername(secretForm.elements.name.value);
  const letters = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ";
  const digits = "23456789";
  const all = letters + digits;
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  const suffix = [
    letters[bytes[0] % letters.length],
    digits[bytes[1] % digits.length],
    ...Array.from(bytes.slice(2), value => all[value % all.length]),
  ];
  for (let i = suffix.length - 1; i > 0; i--) {
    const index = bytes[i % bytes.length] % (i + 1);
    [suffix[i], suffix[index]] = [suffix[index], suffix[i]];
  }
  secretForm.elements.password.value = `${prefix}${suffix.join("")}`;
}

async function copyText(value, label) {
  if (!value) {
    toast(`${label} masih kosong`, true);
    return;
  }
  try {
    if (!navigator.clipboard?.writeText) throw new Error("Clipboard API tidak tersedia");
    await navigator.clipboard.writeText(value);
    toast(`${label} disalin`);
    return true;
  } catch {
    const field = document.createElement("textarea");
    field.value = value;
    field.style.position = "fixed";
    field.style.left = "-9999px";
    field.style.opacity = "0";
    document.body.appendChild(field);
    let copied = false;
    try {
      field.focus();
      field.select();
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    } finally {
      field.remove();
    }
    if (copied) {
      toast(`${label} disalin`);
      return true;
    } else {
      toast(`Gagal menyalin ${label.toLowerCase()}`, true);
      return false;
    }
  }
}

$("#btn-generate-password").onclick = randomPassword;
secretForm.elements.name.oninput = updatePasswordFromUsername;
$("#btn-copy-credentials").onclick = async () => {
  const username = secretForm.elements.name.value.trim();
  const password = secretForm.elements.password.value;
  if (!username || !password) {
    toast("Isi username terlebih dahulu agar password dapat dibuat otomatis.", true);
    return;
  }
  const button = $("#btn-copy-credentials");
  setBusy(button, true);
  try {
    await copyText(`Username: ${username}\nPassword: ${password}`, "Username dan password");
  } finally {
    setBusy(button, false);
  }
};
secretForm.elements.profile.onchange = updateGeneratedComment;
secretForm.elements.comment.oninput = () => { commentEdited = true; };

secretForm.onsubmit = async event => {
  event.preventDefault();
  const button = $("#btn-save-secret"), f = secretForm.elements;
  setBusy(button, true);
  try {
    await api(`/api/mikrotik/routers/${routerId()}/secrets`, {method: "POST", body: {
      name: f.name.value.trim(), password: f.password.value, profile: f.profile.value,
      comment: f.comment.value.trim(),
    }});
    secretDialog.close();
    toast("PPPoE user berhasil ditambahkan");
    await loadData();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
};

editForm.onsubmit = async event => {
  event.preventDefault();
  const button = $("#btn-save-edit-secret"), f = editForm.elements;
  const body = {profile: f.profile.value, comment: f.comment.value.trim(), disabled: f.disabled.checked};
  if (f.password.value) body.password = f.password.value;
  setBusy(button, true);
  try {
    await api(`/api/mikrotik/routers/${routerId()}/secrets/${encodeURIComponent(editingId)}`, {method: "PATCH", body});
    editDialog.close();
    toast("PPPoE user berhasil diperbarui");
    await loadData();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
};

profileForm.onsubmit = async event => {
  event.preventDefault();
  const button = $("#btn-save-change-profile");
  setBusy(button, true);
  try {
    await api(`/api/mikrotik/routers/${routerId()}/secrets/${encodeURIComponent(profileId)}/change-profile`, {
      method: "POST", body: {profile: $("#cp-select").value},
    });
    profileDialog.close();
    toast("Profile PPPoE berhasil diganti");
    await loadData();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
};

async function lookupOnu(username) {
  const dialog = $("#onu-lookup-dialog"), link = $("#lookup-onu-link");
  $("#lookup-user").textContent = username;
  $("#lookup-mac").textContent = "Mencari…";
  $("#lookup-message").textContent = "—";
  $("#lookup-sn").textContent = "—";
  $("#lookup-position").textContent = "—";
  link.hidden = true;
  dialog.showModal();
  try {
    const data = await api(`/api/mikrotik/routers/${routerId()}/mac-lookup?username=${encodeURIComponent(username)}`);
    $("#lookup-mac").textContent = data.mac || "Tidak ada sesi aktif";
    $("#lookup-message").textContent = data.message;
    if (data.onu) {
      $("#lookup-sn").textContent = data.onu.sn || "—";
      $("#lookup-position").textContent = `Slot ${data.onu.slot} / PON ${data.onu.pon} / ONU #${data.onu.onu_number}`;
      if (data.onu.onu_id) {
        link.href = `/onus/${data.onu.onu_id}`;
        link.hidden = false;
      }
    }
  } catch (error) {
    $("#lookup-message").textContent = error.message;
  }
}

$$(".btn-dlg-cancel").forEach(button => {
  button.onclick = () => button.closest("dialog").close();
});
$("#btn-close-lookup").onclick = () => $("#onu-lookup-dialog").close();

routerSelect.onchange = async () => {
  SELECTED_ROUTER = ROUTERS.find(r => r.id === selectedRouterId()) || null;
  if (!SELECTED_ROUTER) return;
  sessionStorage.setItem(routerKey, SELECTED_ROUTER.id);
  $("#pppoe-search").value = "";
  try {
    await loadData();
  } catch (error) {
    SECRETS = ACTIVE = PROFILES = [];
    render();
    toast(`Gagal memuat data router: ${error.message}`, true);
  }
};
$("#pppoe-search").oninput = render;
$("#btn-pppoe-refresh").onclick = async event => {
  const button = event.currentTarget;
  setBusy(button, true);
  try {
    await loadRouters();
    toast("Data PPPoE diperbarui");
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
};

loadRouters().catch(error => {
  toast(`Gagal memuat router: ${error.message}`, true);
  $("#pppoe-empty").hidden = false;
  $("#pppoe-empty").textContent = "Tidak dapat memuat daftar router. Periksa koneksi lalu refresh.";
});
