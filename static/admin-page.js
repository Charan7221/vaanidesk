// Platform admin: clinics on this server.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : `HTTP ${r.status}`);
    return d;
  };
  async function load() {
    const rows = await api("/api/admin/clinics");
    $("clinics").innerHTML = rows.map(c => `<tr>
      <td><b>${esc(c.name)}</b><div class="small muted">${esc(c.id)}${c.active ? "" : ' · <span class="badge bad">inactive</span>'}</div></td>
      <td><input data-numbers="${esc(c.id)}" value="${esc(c.phone_numbers.join(", "))}" placeholder="+91…" /></td>
      <td>${c.this_month.calls}</td><td>${c.this_month.minutes}</td><td>${c.this_month.appointments}</td><td>${c.this_month.opd_tokens}</td><td>${c.staff}</td>
      <td class="nowrap"><button class="mini primary" data-open="${esc(c.id)}">Open</button>
        <a class="small" href="/demo?clinic=${encodeURIComponent(c.id)}" target="_blank">Demo</a>
        ${c.id === "default" ? "" : `<button class="mini" data-active="${esc(c.id)}" data-to="${c.active ? 0 : 1}">${c.active ? "Deactivate" : "Activate"}</button>`}</td></tr>`).join("");
    document.querySelectorAll("[data-numbers]").forEach(i => i.onchange = async () => {
      try { await api(`/api/admin/clinics/${i.dataset.numbers}`, { method: "POST", body: JSON.stringify({ phone_numbers: i.value.split(",").map(s => s.trim()).filter(Boolean) }) }); i.classList.remove("bad-input"); }
      catch (ex) { alert(ex.message); i.classList.add("bad-input"); }
    });
    document.querySelectorAll("[data-open]").forEach(b => b.onclick = async () => { await api(`/api/admin/switch/${b.dataset.open}`, { method: "POST" }); location.href = "/dashboard"; });
    document.querySelectorAll("[data-active]").forEach(b => b.onclick = async () => {
      if (b.dataset.to === "0" && !confirm("Deactivate this clinic? Its staff can't log in and calls go to the first clinic.")) return;
      await api(`/api/admin/clinics/${b.dataset.active}`, { method: "POST", body: JSON.stringify({ active: b.dataset.to === "1" }) }); load();
    });
  }
  $("newClinic").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    f.phone_numbers = (f.phone_numbers || "").split(",").map(s => s.trim()).filter(Boolean);
    try { await api("/api/admin/clinics", { method: "POST", body: JSON.stringify(f) }); e.target.reset(); $("err").textContent = ""; load(); }
    catch (ex) { $("err").textContent = ex.message; }
  };
  load().catch(ex => ($("clinics").innerHTML = `<tr><td colspan="8">${esc(ex.message)}</td></tr>`));
})();
