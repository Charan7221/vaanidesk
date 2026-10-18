// Clinic calendar: week view, block time, staff bookings, cancel, subscription link.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
  };
  const toMin = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; };
  const toHHMM = (min) => `${String(Math.floor(min / 60)).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;
  const nice = (hhmm) => { const m = toMin(hhmm); const h = Math.floor(m / 60); return `${((h + 11) % 12) + 1}:${String(m % 60).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; };
  const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

  let weekStart = new Date(); weekStart.setHours(0, 0, 0, 0);
  let data = null;
  let providerId = "";

  // ------------------------------------------------------------ rendering
  function cellsFor(day) {
    // map "HH:MM" -> {type, item} for this day and the selected provider
    const map = {};
    const step = data.slot_minutes;
    const mark = (startIso, endIso, type, item) => {
      if (startIso.slice(0, 10) !== day.date && endIso.slice(0, 10) !== day.date) return;
      const s = startIso.slice(0, 10) === day.date ? toMin(startIso.slice(11, 16)) : 0;
      const e = endIso.slice(0, 10) === day.date ? toMin(endIso.slice(11, 16)) : 24 * 60;
      for (let t = Math.floor(s / step) * step; t < e; t += step) {
        const key = toHHMM(t);
        if (!map[key] || type === "appt") map[key] = { type, item, first: t <= s, last: t + step >= e };
      }
    };
    data.blocks.forEach(b => mark(b.start, b.end, "block", b));
    data.appointments.forEach(a => mark(a.start, a.end, "appt", a));
    return map;
  }

  function render() {
    const step = data.slot_minutes;
    let lo = 24 * 60, hi = 0;
    data.days.forEach(d => d.open.forEach(([a, b]) => { lo = Math.min(lo, toMin(a)); hi = Math.max(hi, toMin(b)); }));
    if (lo >= hi) { lo = 9 * 60; hi = 18 * 60; }
    const times = [];
    for (let t = lo; t < hi; t += step) times.push(toHHMM(t));

    const first = data.days[0].date, last = data.days[data.days.length - 1].date;
    $("range").textContent = `${new Date(first + "T00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" })} – ${new Date(last + "T00:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`;

    const todayIso = isoDate(new Date());
    let html = `<div class="cal-row cal-head"><div class="cal-time"></div>` +
      data.days.map(d => `<div class="cal-day ${d.date === todayIso ? "is-today" : ""}">${esc(d.weekday)} <span>${Number(d.date.slice(8))}</span></div>`).join("") + `</div>`;

    const maps = data.days.map(cellsFor);
    const now = new Date();
    times.forEach(t => {
      html += `<div class="cal-row"><div class="cal-time">${nice(t)}</div>`;
      data.days.forEach((d, i) => {
        const open = d.open.some(([a, b]) => toMin(t) >= toMin(a) && toMin(t) + step <= toMin(b));
        const c = maps[i][t];
        const past = new Date(`${d.date}T${t}`) < now;
        if (c && c.type === "appt") {
          // Each booking is drawn as its own card, so back-to-back bookings don't merge into one bar.
          const a = c.item, from = nice(a.start.slice(11, 16)), to = nice(a.end.slice(11, 16));
          const cls = `cell appt${c.first ? " start" : ""}${c.last ? " end" : ""}`;
          html += `<button class="${cls}" data-kind="appt" data-id="${esc(a.id)}" title="${esc(`${a.patient_name} · ${from}–${to}`)}">${c.first ? `<span class="t">${from.replace(/ (AM|PM)$/, "")}</span> ${esc(a.patient_name)}` : ""}</button>`;
        } else if (c && c.type === "block") {
          html += `<button class="cell block" data-kind="block" data-id="${esc(c.item.id)}" title="${esc(c.item.reason || "Blocked")}">${c.first ? esc(c.item.reason || "Blocked") : ""}</button>`;
        } else if (!open) {
          html += `<div class="cell closed"></div>`;
        } else if (past) {
          html += `<div class="cell past"></div>`;
        } else {
          html += `<button class="cell free" data-kind="free" data-date="${d.date}" data-time="${t}" aria-label="Free ${d.date} ${t}"></button>`;
        }
      });
      html += `</div>`;
    });
    const grid = $("grid");
    grid.style.setProperty("--days", data.days.length);
    grid.innerHTML = html;
  }

  async function load() {
    const q = new URLSearchParams({ start: isoDate(weekStart), days: "7" });
    if (providerId) q.set("provider_id", providerId);
    data = await api(`/api/calendar?${q}`);
    if (!$("provider").options.length) {
      $("provider").innerHTML = data.providers.map(p => `<option value="${esc(p.id)}">${esc(p.name)}${p.specialty ? " · " + esc(p.specialty) : ""}</option>`).join("");
      providerId = $("provider").value;
      return load();
    }
    render();
  }

  // ------------------------------------------------------------ dialogs
  const dlg = $("dlg");
  function open(title, body, onSubmit) {
    $("dlgTitle").textContent = title;
    $("dlgBody").innerHTML = body;
    dlg.showModal();
    $("dlgBody").querySelectorAll("[data-action]").forEach(btn => btn.addEventListener("click", async (e) => {
      e.preventDefault();
      const err = $("dlgBody").querySelector(".err");
      try { await onSubmit(btn.dataset.action, new FormData($("dlgForm"))); dlg.close(); await load(); }
      catch (ex) { if (err) err.textContent = ex.message; }
    }));
  }

  function freeSlot(date, time) {
    const step = data.slot_minutes;
    const day = data.days.find(d => d.date === date);
    const closeMin = Math.max(...day.open.map(([, b]) => toMin(b)));
    const ends = [];
    for (let t = toMin(time) + step; t <= closeMin; t += step) ends.push(toHHMM(t));
    const doc = data.providers.find(p => p.id === providerId);
    const when = `${new Date(date + "T00:00").toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short" })}, ${nice(time)}`;
    open(when, `
      <div class="tabs">
        <fieldset class="stack">
          <legend>Block this time</legend>
          <label class="field">Until <select name="end">${ends.map(e => `<option value="${e}">${nice(e)}</option>`).join("")}</select></label>
          <label class="field">Reason <input name="reason" placeholder="e.g. Meeting, lunch, leave" /></label>
          <label class="check"><input type="checkbox" name="clinic" /> Whole clinic (all doctors)</label>
          <button class="primary" data-action="block">Block time</button>
        </fieldset>
        <fieldset class="stack">
          <legend>Book an appointment with ${esc(doc ? doc.name : "")}</legend>
          <label class="field">Patient name <input name="patient_name" /></label>
          <label class="field">Phone <input name="phone" type="tel" placeholder="+91…" /></label>
          <label class="field">Reason <input name="reason_appt" /></label>
          <button data-action="book">Book</button>
        </fieldset>
      </div>
      <p class="err small"></p>`,
    async (action, f) => {
      if (action === "block") {
        await api("/api/blocks", { method: "POST", body: JSON.stringify({ date, start: time, end: f.get("end"), reason: f.get("reason"), provider_id: f.get("clinic") ? null : providerId }) });
      } else {
        if (!f.get("patient_name").trim()) throw new Error("Enter the patient's name");
        await api("/api/appointments", { method: "POST", body: JSON.stringify({ date, time, provider_id: providerId, patient_name: f.get("patient_name"), phone: f.get("phone"), reason: f.get("reason_appt") }) });
      }
    });
  }

  function showAppt(id) {
    const a = data.appointments.find(x => x.id === id);
    open(a.patient_name, `
      <dl class="kv">
        <dt>When</dt><dd>${new Date(a.start.slice(0, 10) + "T00:00").toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short" })}, ${nice(a.start.slice(11, 16))} – ${nice(a.end.slice(11, 16))}</dd>
        <dt>Doctor</dt><dd>${esc(a.provider)}</dd>
        <dt>Phone</dt><dd>${esc(a.phone || "–")}</dd>
        <dt>Reason</dt><dd>${esc(a.reason || "–")}</dd>
        <dt>Booked by</dt><dd>${esc(a.booked_by)}</dd>
        <dt>Ref</dt><dd>${esc(a.id)}</dd>
      </dl>
      <button class="danger" data-action="cancel">Cancel appointment</button><p class="err small"></p>`,
    async () => { if (!confirm("Cancel this appointment?")) throw new Error("Not cancelled"); await api(`/api/appointments/${id}/cancel`, { method: "POST" }); });
  }

  function showBlock(id) {
    const b = data.blocks.find(x => x.id === id);
    open(b.reason || "Blocked time", `
      <dl class="kv">
        <dt>When</dt><dd>${new Date(b.start.slice(0, 10) + "T00:00").toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short" })}, ${nice(b.start.slice(11, 16))} – ${nice(b.end.slice(11, 16))}</dd>
        <dt>For</dt><dd>${esc(b.provider)}</dd>
      </dl>
      <button class="danger" data-action="remove">Remove block</button><p class="err small"></p>`,
    async () => api(`/api/blocks/${id}`, { method: "DELETE" }));
  }

  $("grid").addEventListener("click", (e) => {
    const cell = e.target.closest("button.cell");
    if (!cell) return;
    if (cell.dataset.kind === "free") freeSlot(cell.dataset.date, cell.dataset.time);
    else if (cell.dataset.kind === "appt") showAppt(cell.dataset.id);
    else if (cell.dataset.kind === "block") showBlock(cell.dataset.id);
  });

  // ------------------------------------------------------------ controls
  $("prev").onclick = () => { weekStart.setDate(weekStart.getDate() - 7); load(); };
  $("next").onclick = () => { weekStart.setDate(weekStart.getDate() + 7); load(); };
  $("today").onclick = () => { weekStart = new Date(); weekStart.setHours(0, 0, 0, 0); load(); };
  $("provider").onchange = (e) => { providerId = e.target.value; load(); };

  api("/api/calendar-feed").then(f => { $("feedUrl").value = f.url; $("feedWarn").hidden = f.public; });
  $("copyFeed").onclick = async () => {
    try { await navigator.clipboard.writeText($("feedUrl").value); $("copyFeed").textContent = "Copied"; }
    catch (_) { $("feedUrl").select(); }
    setTimeout(() => ($("copyFeed").textContent = "Copy link"), 1500);
  };
  fetch("/api/public-config").then(r => r.json()).then(c => ($("bizName").textContent = c.business));
  load().catch(e => ($("grid").innerHTML = `<p class="muted">Could not load calendar: ${esc(e.message)}</p>`));
  setInterval(() => { if (!dlg.open) load(); }, 30000);
})();
