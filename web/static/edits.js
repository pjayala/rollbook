// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Session edits dialog (data/edits.json overrides). */

// ------------------------------------------------------------------ edits

// Stored server-side as overrides (data/edits.json), applied to every query and
// kept across `db build --fresh`; the DB itself stays read-only.
async function postEdit(body) {
  const r = await fetch("/api/edit", { method: "POST", headers: { "X-Rollbook": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
async function openEdit(a) {
  await loadGear(true).catch(() => null);  // the gear list and its rev as they are now
  const dlg = $("#edit-dlg"), msg = $("#ed-msg"), e = a.edit || {};
  $("#ed-name").value = a.name || ""; $("#ed-name").placeholder = a.orig_name || "";
  $("#ed-type").value = a.type || ""; $("#ed-type").placeholder = a.orig_type || "";
  $("#ed-name-orig").textContent = e.name ? `original: ${a.orig_name || "–"} (clear to restore)` : "";
  $("#ed-type-orig").textContent = e.type ? `original: ${a.orig_type || "–"} (clear to restore)` : "e.g. HUAWEI records skating as cycling";
  $("#ed-types").innerHTML = S.types.map(t => `<option value="${esc(t.type)}">`).join("");
  $("#ed-hidden").checked = !!a.hidden;
  $("#ed-rmrow").hidden = !a.import_file; $("#ed-rmfile").checked = false;
  $("#ed-delete").disabled = !!a.deleted;
  msg.textContent = "";
  const gearChange = editGearField(a);
  const save = async (body, dest, gearOp) => {
    msg.className = "small muted"; msg.textContent = "saving…";
    try {
      if (gearOp) await postGear(gearOp);  // first: the likelier one to fail (a stale tab, a bad store)
      const j = await postEdit({ id: String(a.id), ...body }).catch(e => { throw new Error(gearOp ? `gear saved, the rest not: ${e.message}` : e.message); });
      dlg.close();
      const type = (j.edit && j.edit.type) || a.orig_type;
      if (type && type !== S.type && !isAll() && !j.edit.hidden) S.type = localStorage.type = type;  // follow the session to its sport
      await refreshData(dest);
    } catch (err) { msg.className = "small down"; msg.textContent = "Could not save: " + err.message; }
  };
  $("#ed-form").onsubmit = ev => { ev.preventDefault(); save({ name: $("#ed-name").value, type: $("#ed-type").value, hidden: $("#ed-hidden").checked }, undefined, gearChange()); };
  $("#ed-delete").onclick = () => {
    const rm = $("#ed-rmfile").checked;
    if (!confirm(`Delete "${a.name}"?\n\n` + (rm ? "Its imported file is removed from data/imports for good; the session can still be restored until the next fresh rebuild."
      : "It disappears from stats, lists, maps and segments. Restore it from Sessions → show hidden."))) return;
    save({ delete: true, remove_file: rm }, "#/sessions");
  };
  $("#ed-close").onclick = () => dlg.close();
  dlg.showModal();
  $("#ed-name").focus();
}
