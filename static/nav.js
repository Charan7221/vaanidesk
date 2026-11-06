// App shell for staff pages: sidebar navigation, clinic name / switcher, signed-in user, page title.
(async () => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const r = await fetch("/api/me");
  if (r.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); return; }
  const me = await r.json();
  window.VD_ME = me;

  const svg = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
  const I = {
    home: svg('<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>'),
    chart: svg('<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M22 20H2"/>'),
    ticket: svg('<path d="M3 8a2 2 0 0 0 2-2h14a2 2 0 0 0 2 2v2a2 2 0 0 0 0 4v2a2 2 0 0 0-2 2H5a2 2 0 0 0-2-2v-2a2 2 0 0 0 0-4z"/><path d="M13 6v12" stroke-dasharray="2 2"/>'),
    cal: svg('<rect x="3" y="4.5" width="18" height="16" rx="2"/><path d="M3 9.5h18M8 2.5v4M16 2.5v4"/>'),
    book: svg('<path d="M4 4.5A2.5 2.5 0 0 1 6.5 2H20v17H6.5A2.5 2.5 0 0 0 4 21.5z"/><path d="M4 21.5V4.5"/>'),
    gear: svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
    building: svg('<rect x="4" y="2.5" width="16" height="19" rx="1.5"/><path d="M9 21.5v-4h6v4M8.5 6.5h.01M12 6.5h.01M15.5 6.5h.01M8.5 10.5h.01M12 10.5h.01M15.5 10.5h.01M8.5 14h.01M12 14h.01M15.5 14h.01"/>'),
    chat: svg('<path d="M21 15a2 2 0 0 1-2 2H8l-5 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
    out: svg('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/>'),
    menu: svg('<path d="M3 6h18M3 12h18M3 18h18"/>'),
    logo: svg('<path d="M12 3a9 9 0 0 0-9 9v4a2 2 0 0 0 2 2h1v-6H5a7 7 0 0 1 14 0h-1v6h1a2 2 0 0 0 2-2v-4a9 9 0 0 0-9-9z"/>'),
  };
  const PAGES = {
    "/dashboard": ["Overview", "Calls, bookings and anything that needs your attention."],
    "/report": ["Results", "What VaaniDesk did for your clinic."],
    "/opd": ["OPD queue", "Walk-in tokens for every doctor and session."],
    "/calendar": ["Calendar", "Appointments and blocked time, per doctor."],
    "/knowledge": ["Knowledge", "Documents the receptionist answers questions from."],
    "/setup": ["Setup", "Your clinic profile, doctors, fees, settings and staff."],
    "/admin": ["Clinics", "Every clinic on this server, with this month's usage."],
  };
  const here = location.pathname;
  const owner = me.role === "owner" || me.role === "superadmin";
  const link = (href, icon, text, extra = "") =>
    `<a class="sb-link" href="${href}"${here === href ? ' aria-current="page"' : ""}${extra}>${I[icon]}<span>${text}</span></a>`;
  const initials = (me.name || me.email).split(/[\s@.]+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join("");
  const roleName = { owner: "Owner", staff: "Receptionist", superadmin: "Platform admin" }[me.role] || me.role;
  const switcher = me.role === "superadmin" && me.clinics && me.clinics.length > 1
    ? `<select id="clinicSwitch" aria-label="Clinic">${me.clinics.map(c => `<option value="${esc(c.id)}"${c.id === me.clinic_id ? " selected" : ""}>${esc(c.name)}</option>`).join("")}</select>` : "";

  const header = document.querySelector("header.top");
  if (!header) return;
  header.innerHTML = `
    <div class="sb-brand"><span class="sb-logo">${I.logo}</span>VaaniDesk</div>
    <div class="sb-clinic"><b>${esc(me.clinic_name)}</b><span>${me.pilot_mode ? "Pilot mode · staff confirm bookings" : "Live"}</span>${switcher}</div>
    <nav aria-label="Main">
      <div class="sb-group">Clinic</div>
      ${link("/dashboard", "home", "Overview")}${link("/report", "chart", "Results")}
      ${link("/opd", "ticket", "OPD queue")}${link("/calendar", "cal", "Calendar")}${link("/knowledge", "book", "Knowledge")}
      ${owner ? `<div class="sb-group">Manage</div>${link("/setup", "gear", "Setup")}` : ""}
      ${me.role === "superadmin" ? link("/admin", "building", "Clinics") : ""}
      <div class="sb-group">Try it</div>
      ${link(`/demo?clinic=${encodeURIComponent(me.clinic_id)}`, "chat", "Talk to the receptionist", ' target="_blank"')}
    </nav>
    <div class="sb-foot"><span class="sb-avatar">${esc(initials)}</span>
      <div class="sb-who"><b>${esc(me.name || me.email)}</b><span>${esc(roleName)}</span></div>
      <button class="sb-logout" id="logout" title="Log out" aria-label="Log out">${I.out}</button></div>`;
  header.style.display = "flex";
  document.body.classList.add("shell");
  document.body.insertAdjacentHTML("afterbegin", `<div class="sb-toggle"><button aria-label="Menu" id="sbMenu">${I.menu}</button><span>${esc(me.clinic_name)}</span></div>`);
  document.getElementById("sbMenu").onclick = () => document.body.classList.toggle("sb-open");
  document.addEventListener("click", (e) => {
    if (document.body.classList.contains("sb-open") && !e.target.closest("header.top, .sb-toggle")) document.body.classList.remove("sb-open");
  });

  const page = PAGES[here];
  const main = document.querySelector("main");
  if (page && main && !main.querySelector(".page-title")) {
    main.insertAdjacentHTML("afterbegin", `<div class="page-title"><h1>${page[0]}</h1><p>${page[1]}</p></div>`);
  }
  document.title = `${page ? page[0] : "VaaniDesk"} · ${me.clinic_name}`;

  document.getElementById("logout").onclick = async () => {
    await fetch("/api/logout", { method: "POST" });
    location.href = "/login";
  };
  const sw = document.getElementById("clinicSwitch");
  if (sw) sw.onchange = async () => { await fetch(`/api/admin/switch/${encodeURIComponent(sw.value)}`, { method: "POST" }); location.reload(); };
})();
