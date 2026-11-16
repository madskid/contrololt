const rid = window.ROUTER_ID;
let ROUTER = null, SECRETS = [], ACTIVE = [], PROFILES = [];

// ── Tab Navigation ────────────────────────────────────────────────────────────
const tabs = $$("#tabs button");
tabs.forEach(btn => {
  btn.onclick = () => {
    tabs.forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    $$(".tab").forEach(t => t.hidden = true);
    $(`#${btn.dataset.tab}`).hidden = false;
  };
});

// Cancel buttons on any dialog
$$(".btn-dlg-cancel").forEach(b => {
  b.onclick = () => b.closest("dialog").close();
});

// ── Load router metadata & status ─────────────────────────────────────────────
async function loadRouter() {
  const list = await api("/api/mikrotik/routers");
  ROUTER = list.find(x => x.id === rid);
  if (!ROUTER) {
    toast("Router tidak ditemukan", true);
    return;
  }
  $("#router-name").textContent = ROUTER.name;
  $("#router-status-badge").innerHTML = badge(ROUTER.status);
  $("#router-sub").textContent = `${ROUTER.ip}:${ROUTER.api_port} | Identity: ${ROUTER.identity || "—"} | Model: ${ROUTER.board || "—"} | ROS: ${ROUTER.ros_version || "—"} | Uptime: ${ROUTER.uptime || "—"}`;
}

$("#btn-recheck").onclick = async () => {
  const btn = $("#btn-recheck");
  btn.disabled = true;
  try {
    const res = await api(`/api/mikrotik/routers/${rid}/check`, {method: "POST"});
    if (res.ok) {
      toast("Status router berhasil diperbarui");
      await loadRouter();
    } else {
      toast(`Gagal connect router: ${res.detail}`, true);
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

// ── Profiles ──────────────────────────────────────────────────────────────────
async function loadProfiles() {
  try {
    PROFILES = await api(`/api/mikrotik/routers/${rid}/profiles`);
    renderProfiles();
    populateProfileSelects();
  } catch (e) {
    toast(`Gagal mengambil profile: ${e.message}`, true);
  }
}

function renderProfiles() {
  $("#profile-table tbody").innerHTML = PROFILES.map(p => `
    <tr>
      <td><b>${esc(p.name)}</b></td>
      <td>${esc(p.rate_limit || "—")}</td>
      <td>${esc(p.local_address || "—")}</td>
      <td>${esc(p.remote_address || "—")}</td>
    </tr>
  `).join("");
  $("#profile-empty").hidden = PROFILES.length > 0;
}

function populateProfileSelects() {
  const opts = PROFILES.map(p => `<option value="${esc(p.name)}">${esc(p.name)}${p.rate_limit ? ' (' + esc(p.rate_limit) + ')' : ''}</option>`).join("");
  const s1 = $("#secret-form select[name='profile']");
  if (s1) s1.innerHTML = opts;
  const s2 = $("#secret-edit-form select[name='profile']");
  if (s2) s2.innerHTML = opts;
  const s3 = $("#cp-select");
  if (s3) s3.innerHTML = opts;
}

$("#btn-refresh-profiles").onclick = async () => {
  const btn = $("#btn-refresh-profiles");
  btn.disabled = true;
  try {
    await loadProfiles();
    toast("Profiles disinkronkan");
  } finally {
    btn.disabled = false;
  }
};

// ── PPPoE Users (Secrets) ─────────────────────────────────────────────────────
async function loadSecrets() {
  try {
    SECRETS = await api(`/api/mikrotik/routers/${rid}/secrets`);
    renderSecrets();
  } catch (e) {
    toast(`Gagal memuat secrets: ${e.message}`, true);
  }
}

function renderSecrets() {
  const q = ($("#f-secret-q").value || "").toLowerCase();
  const list = SECRETS.filter(s =>
    !q || [s.name, s.profile, s.comment].some(x => (x || "").toLowerCase().includes(q))
  );

  const isAdmin = window.USER_ROLE !== "read_only";

  $("#secret-table tbody").innerHTML = list.map(s => `
    <tr data-id="${esc(s.id)}" data-name="${esc(s.name)}" data-profile="${esc(s.profile)}" data-comment="${esc(s.comment)}">
      <td><b>${esc(s.name)}</b></td>
      <td><span class="badge" style="background:#4b5563">${esc(s.profile)}</span></td>
      <td>${esc(s.comment || "—")}</td>
      <td>${s.disabled ? '<span class="badge st-offline">Disabled</span>' : '<span class="badge st-online">Enabled</span>'}</td>
      <td class="row">
        <button class="sm" data-act="lookup" title="Cek MAC & ONU">🔍 Cek OLT</button>
        ${isAdmin ? `
        <button class="sm" data-act="ch-prof" title="Ganti Profile">Profile</button>
        <button class="sm" data-act="copy" title="Copy User">Copy</button>
        <button class="sm" data-act="edit" title="Edit">Edit</button>
        <button class="sm danger" data-act="del" title="Hapus">Delete</button>
        ` : ''}
      </td>
    </tr>
  `).join("");
  $("#secret-empty").hidden = list.length > 0;
}

$("#btn-refresh-secrets").onclick = loadSecrets;
$("#f-secret-q").oninput = renderSecrets;

// Add Secret Dialog
const dlgSecret = $("#secret-dialog"), formSecret = $("#secret-form");
const btnAddSecret = $("#btn-add-secret");
if (btnAddSecret) {
  btnAddSecret.onclick = () => {
    formSecret.reset();
    populateProfileSelects();
    dlgSecret.showModal();
  };
}

formSecret.onsubmit = async ev => {
  ev.preventDefault();
  const btn = $("#btn-save-secret");
  btn.disabled = true;
  try {
    const f = formSecret.elements;
    const body = {
      name: f.name.value.trim(),
      password: f.password.value,
      profile: f.profile.value,
      comment: f.comment.value.trim(),
    };
    await api(`/api/mikrotik/routers/${rid}/secrets`, {method: "POST", body});
    dlgSecret.close();
    toast(`User PPPoE ${body.name} berhasil ditambahkan`);
    await loadSecrets();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

// Edit Secret Dialog
let editingSecretId = null;
const dlgEditSecret = $("#secret-edit-dialog"), formEditSecret = $("#secret-edit-form");

function openEditSecret(tr) {
  editingSecretId = tr.dataset.id;
  formEditSecret.reset();
  populateProfileSelects();
  $("#edit-username").textContent = tr.dataset.name;
  formEditSecret.elements.profile.value = tr.dataset.profile;
  formEditSecret.elements.comment.value = tr.dataset.comment;
  dlgEditSecret.showModal();
}

formEditSecret.onsubmit = async ev => {
  ev.preventDefault();
  const btn = $("#btn-save-edit-secret");
  btn.disabled = true;
  try {
    const f = formEditSecret.elements;
    const body = {
      profile: f.profile.value,
      comment: f.comment.value.trim(),
      disabled: f.disabled.checked,
    };
    if (f.password.value) body.password = f.password.value;
    await api(`/api/mikrotik/routers/${rid}/secrets/${encodeURIComponent(editingSecretId)}`, {
      method: "PATCH", body,
    });
    dlgEditSecret.close();
    toast("User PPPoE berhasil diperbarui");
    await loadSecrets();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

// Copy Secret Dialog
let copyingSecretId = null;
const dlgCopySecret = $("#secret-copy-dialog"), formCopySecret = $("#secret-copy-form");

function openCopySecret(tr) {
  copyingSecretId = tr.dataset.id;
  formCopySecret.reset();
  $("#copy-orig-name").textContent = tr.dataset.name;
  dlgCopySecret.showModal();
}

formCopySecret.onsubmit = async ev => {
  ev.preventDefault();
  const btn = $("#btn-save-copy-secret");
  btn.disabled = true;
  try {
    const f = formCopySecret.elements;
    const body = {
      new_name: f.new_name.value.trim(),
      new_password: f.new_password.value,
    };
    await api(`/api/mikrotik/routers/${rid}/secrets/${encodeURIComponent(copyingSecretId)}/copy`, {
      method: "POST", body,
    });
    dlgCopySecret.close();
    toast(`User berhasil disalin sebagai ${body.new_name}`);
    await loadSecrets();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

// Change Profile Dialog
let changeProfileSecretId = null;
const dlgChangeProfile = $("#change-profile-dialog"), formChangeProfile = $("#change-profile-form");

function openChangeProfile(tr) {
  changeProfileSecretId = tr.dataset.id;
  $("#cp-username").textContent = tr.dataset.name;
  populateProfileSelects();
  $("#cp-select").value = tr.dataset.profile;
  dlgChangeProfile.showModal();
}

formChangeProfile.onsubmit = async ev => {
  ev.preventDefault();
  const btn = $("#btn-save-change-profile");
  btn.disabled = true;
  try {
    const profile = $("#cp-select").value;
    await api(`/api/mikrotik/routers/${rid}/secrets/${encodeURIComponent(changeProfileSecretId)}/change-profile`, {
      method: "POST", body: {profile},
    });
    dlgChangeProfile.close();
    toast("Profile berhasil diubah");
    await loadSecrets();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

// Secret Table Actions
$("#secret-table").addEventListener("click", async ev => {
  const b = ev.target.closest("button[data-act]");
  if (!b) return;
  const tr = b.closest("tr");
  const act = b.dataset.act;

  if (act === "lookup") {
    // Switch to MAC lookup tab and run search
    const username = tr.dataset.name;
    const tabBtn = $(`#tabs button[data-tab="tab-mac-lookup"]`);
    if (tabBtn) tabBtn.click();
    $("#lookup-user").value = username;
    runMacLookup(username);
  } else if (act === "edit") {
    openEditSecret(tr);
  } else if (act === "copy") {
    openCopySecret(tr);
  } else if (act === "ch-prof") {
    openChangeProfile(tr);
  } else if (act === "del") {
    if (confirm(`Hapus user PPPoE "${tr.dataset.name}" dari MikroTik?`)) {
      try {
        await api(`/api/mikrotik/routers/${rid}/secrets/${encodeURIComponent(tr.dataset.id)}`, {
          method: "DELETE",
        });
        toast(`User ${tr.dataset.name} berhasil dihapus`);
        await loadSecrets();
      } catch (e) {
        toast(e.message, true);
      }
    }
  }
});

// ── PPPoE Active Sessions ─────────────────────────────────────────────────────
async function loadActive() {
  try {
    ACTIVE = await api(`/api/mikrotik/routers/${rid}/active`);
    renderActive();
  } catch (e) {
    toast(`Gagal memuat sesi aktif: ${e.message}`, true);
  }
}

function renderActive() {
  const q = ($("#f-active-q").value || "").toLowerCase();
  const list = ACTIVE.filter(a =>
    !q || [a.name, a.address, a.caller_id].some(x => (x || "").toLowerCase().includes(q))
  );

  const isAdmin = window.USER_ROLE !== "read_only";

  $("#active-table tbody").innerHTML = list.map(a => `
    <tr data-id="${esc(a.id)}" data-name="${esc(a.name)}" data-mac="${esc(a.caller_id)}">
      <td><b>${esc(a.name)}</b></td>
      <td><code>${esc(a.address || "—")}</code></td>
      <td><code class="mac">${esc(a.caller_id || "—")}</code></td>
      <td>${esc(a.uptime || "—")}</td>
      <td>${esc(a.service || "pppoe")}</td>
      <td class="row">
        <button class="sm" data-act="lookup" title="Cek MAC & ONU">🔍 Cek OLT</button>
        ${isAdmin ? `<button class="sm danger" data-act="disconnect" title="Disconnect agar redial">Disconnect (Redial)</button>` : ''}
      </td>
    </tr>
  `).join("");
  $("#active-empty").hidden = list.length > 0;
}

$("#btn-refresh-active").onclick = loadActive;
$("#f-active-q").oninput = renderActive;

$("#active-table").addEventListener("click", async ev => {
  const b = ev.target.closest("button[data-act]");
  if (!b) return;
  const tr = b.closest("tr");
  const act = b.dataset.act;

  if (act === "lookup") {
    const username = tr.dataset.name;
    const tabBtn = $(`#tabs button[data-tab="tab-mac-lookup"]`);
    if (tabBtn) tabBtn.click();
    $("#lookup-user").value = username;
    runMacLookup(username);
  } else if (act === "disconnect") {
    if (confirm(`Disconnect sesi aktif untuk "${tr.dataset.name}" agar melakukan redial?`)) {
      b.disabled = true;
      try {
        await api(`/api/mikrotik/routers/${rid}/active/${encodeURIComponent(tr.dataset.id)}`, {
          method: "DELETE",
        });
        toast(`Sesi ${tr.dataset.name} diputus, client akan redial`);
        await loadActive();
      } catch (e) {
        toast(e.message, true);
        b.disabled = false;
      }
    }
  }
});

// ── MAC & ONU OLT Automatic Lookup ────────────────────────────────────────────
async function runMacLookup(username) {
  const resBox = $("#lookup-result");
  resBox.hidden = false;
  $("#res-user").textContent = username;
  $("#res-mac").textContent = "Mencari di MikroTik…";
  $("#res-msg").textContent = "—";
  $("#res-sn").textContent = "—";
  $("#res-onu-name").textContent = "—";
  $("#res-onu-type").textContent = "—";
  $("#res-pos").textContent = "—";
  $("#res-status").textContent = "—";
  $("#res-link-wrap").hidden = true;

  try {
    const data = await api(`/api/mikrotik/routers/${rid}/mac-lookup?username=${encodeURIComponent(username)}`);
    $("#res-mac").textContent = data.mac || "(Tidak ada sesi aktif)";
    $("#res-msg").textContent = data.message;

    if (data.onu) {
      const o = data.onu;
      $("#res-sn").innerHTML = `<b>${esc(o.sn)}</b>`;
      $("#res-onu-name").textContent = o.name || "—";
      $("#res-onu-type").textContent = o.onu_type || "—";
      $("#res-pos").textContent = `Slot ${o.slot} / PON ${o.pon} / ONU #${o.onu_number}`;
      $("#res-status").innerHTML = badge(o.status);
      $("#res-link-wrap").hidden = false;
      $("#res-onu-link").href = `/onus/${o.onu_id}`;
    }
  } catch (e) {
    $("#res-msg").textContent = "Error: " + e.message;
  }
}

$("#mac-lookup-form").onsubmit = ev => {
  ev.preventDefault();
  const username = $("#lookup-user").value.trim();
  if (username) runMacLookup(username);
};

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  await loadRouter();
  await Promise.all([loadProfiles(), loadSecrets(), loadActive()]);
}

init().catch(e => toast(e.message, true));
