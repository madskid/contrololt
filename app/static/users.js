const dialog = $("#user-dialog"), form = $("#user-form");
let users = [];

const roleLabel = role => role === "administrator" ? "Administrator" : "Read only";

async function loadUsers() {
  users = await api("/api/users");
  $("#users").innerHTML = users.map(user => `<tr>
    <td>${esc(user.username)}</td><td>${roleLabel(user.role)}</td>
    <td>${user.created_at ? new Date(user.created_at).toLocaleString("en-US") : "—"}</td>
    <td class="row">
      <button class="sm" data-edit="${user.id}">Edit</button>
      <button class="sm danger" data-delete="${user.id}" ${user.username === window.CURRENT_USER ? "disabled" : ""}>Delete</button>
    </td></tr>`).join("");
}

function openUser(user = null) {
  form.reset();
  form.dataset.id = user?.id || "";
  $("#user-title").textContent = user ? `Edit ${user.username}` : "Add user";
  form.elements.username.disabled = !!user;
  form.elements.password.required = !user;
  form.elements.password.value = "";
  form.elements.password.placeholder = user ? "Leave blank to keep the current password" : "";
  form.elements.role.value = user?.role || "read_only";
  form.querySelector("button.primary").textContent = user ? "Save changes" : "Create user";
  dialog.showModal();
}

$("#user-add").onclick = () => openUser();
$("#user-cancel").onclick = () => dialog.close();
$("#users").onclick = async event => {
  const edit = event.target.closest("[data-edit]"), del = event.target.closest("[data-delete]");
  try {
    if (edit) openUser(users.find(user => user.id === +edit.dataset.edit));
    if (del) {
      const user = users.find(row => row.id === +del.dataset.delete);
      if (!user || !confirm(`Delete user "${user.username}"?`)) return;
      await api(`/api/users/${user.id}`, {method: "DELETE"});
      toast("User deleted");
      await loadUsers();
    }
  } catch (error) { toast(error.message, true); }
};

form.onsubmit = async event => {
  event.preventDefault();
  const id = form.dataset.id, password = form.elements.password.value;
  const body = {role: form.elements.role.value};
  if (id) {
    if (password) body.password = password;
  } else {
    body.username = form.elements.username.value;
    body.password = password;
  }
  try {
    await api(id ? `/api/users/${id}` : "/api/users", {method: id ? "PUT" : "POST", body});
    dialog.close();
    toast(id ? "User updated" : "User created");
    await loadUsers();
  } catch (error) { toast(error.message, true); }
};

loadUsers().catch(error => toast(error.message, true));
