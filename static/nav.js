// Shared header for staff pages: clinic name, role-based links, clinic switcher (platform admin), logout.
(async () => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const r = await fetch("/api/me");
  if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); return; }
  const me = await r.json();
  window.VD_ME = me;
  const name = document.getElementById("bizName");
  if (name) name.textContent = me.clinic_name + (me.pilot_mode ? " · pilot" : "");
  const nav = document.querySelector("header.top nav");
  if (!nav) return;
  nav.querySelectorAll('a[href="/demo"]').forEach(a => a.href = `/demo?clinic=${encodeURIComponent(me.clinic_id)}`);
  const here = location.pathname;
  const link = (href, text) => `<a href="${href}"${here === href ? ' aria-current="page"' : ""}>${text}</a>`;
  let extra = "";
  if (me.role === "owner" || me.role === "superadmin") extra += link("/setup", "Setup");
  if (me.role === "superadmin") extra += link("/admin", "Clinics");
  if (me.role === "superadmin" && me.clinics && me.clinics.length > 1) {
    extra += `<select id="clinicSwitch" title="Clinic">${me.clinics.map(c => `<option value="${esc(c.id)}"${c.id === me.clinic_id ? " selected" : ""}>${esc(c.name)}</option>`).join("")}</select>`;
  }
  extra += `<span class="who small muted" title="${esc(me.role)}">${esc(me.name || me.email)}</span><a href="#" id="logout">Log out</a>`;
  nav.insertAdjacentHTML("beforeend", extra);
  document.getElementById("logout").onclick = async (e) => {
    e.preventDefault();
    await fetch("/api/logout", { method: "POST" });
    location.href = "/login";
  };
  const sw = document.getElementById("clinicSwitch");
  if (sw) sw.onchange = async () => { await fetch(`/api/admin/switch/${encodeURIComponent(sw.value)}`, { method: "POST" }); location.reload(); };
})();
