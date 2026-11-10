// Results report page.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const LANG = { "en-IN": "English", "hi-IN": "Hindi", "te-IN": "Telugu", unknown: "Not detected" };
  const kpi = (label, value, note = "") => `<div class="panel kpi"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div>${note ? `<div class="small muted">${esc(note)}</div>` : ""}</div>`;
  const money = (cur, n) => cur + Number(n).toLocaleString("en-IN");
  let config = null;

  function bars(el, rows, key, label, shade) {
    const max = Math.max(1, ...rows.map(r => r[key]));
    el.innerHTML = rows.map((r, i) => `<div class="bar${shade && shade(r) ? " shaded" : ""}" title="${esc(label(r))}: ${r[key]}">
      <span style="height:${Math.round((r[key] / max) * 100)}%"></span><i>${esc(label(r, true, i))}</i></div>`).join("");
  }

  async function load() {
    const r = await fetch(`/api/report?days=${$("days").value}`);
    if (r.status === 401) { location.href = "/login?next=/report"; return; }
    const d = await r.json();
    const fmt = (iso) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    $("period").textContent = `${d.clinic} · ${fmt(d.since)} – ${fmt(d.until)}`;
    $("headline").textContent = d.calls_answered
      ? `VaaniDesk answered ${d.calls_answered} calls. About ${d.would_have_missed} would likely have been missed.`
      : "No calls in this period yet.";
    $("subline").textContent = d.calls_answered
      ? `${d.after_hours_calls} came outside opening hours and ${d.calls_while_another_call_was_on} came while another call was already on.`
      : "";
    $("kpis").innerHTML = [
      kpi("Handled without staff", `${Math.round(d.handled_without_staff_rate * 100)}%`, `${d.handled_without_staff} calls`),
      kpi("Appointments booked", d.appointments_booked_by_ai),
      kpi("OPD tokens given", d.opd_tokens_by_ai),
      kpi("Consultation fees", money(d.currency, d.consultation_fees_from_ai_bookings), "from AI bookings"),
      kpi("Staff time saved", `${(d.staff_minutes_saved / 60).toFixed(1)} h`, `${d.staff_minutes_saved} minutes on the phone`),
      kpi("Reminders sent", d.reminders_sent, "fewer no-shows"),
      kpi("Passed to staff", d.transferred_to_staff + d.callbacks_requested, `${d.transferred_to_staff} transfers · ${d.callbacks_requested} call-backs`),
    ].join("");
    const every = d.days <= 7 ? 1 : d.days <= 31 ? 5 : 15;  // label every Nth day so labels don't overlap
    bars($("byDay"), d.calls_by_day, "calls", (x, short, i) => short ? ((d.days - 1 - i) % every === 0 ? x.date.slice(5).replace("-", "/") : "") : x.date);
    bars($("byHour"), d.calls_by_hour, "calls", (x, short) => short ? (x.hour % 3 === 0 ? String(x.hour) : "") : `${x.hour}:00`,
      (x) => config && !config.openHours.has(x.hour));
    const total = Object.values(d.languages).reduce((a, b) => a + b, 0) || 1;
    $("langs").innerHTML = Object.entries(d.languages).map(([k, v]) => `<div class="lang-row"><span>${esc(LANG[k] || k)}</span>
      <div class="meter"><span style="width:${Math.round(v / total * 100)}%"></span></div><b>${Math.round(v / total * 100)}%</b></div>`).join("") || `<p class="muted small">No calls yet.</p>`;
  }

  // opening hours (any day) to shade the hour chart
  fetch("/api/calendar?days=7").then(r => r.ok ? r.json() : null).then(c => {
    if (!c) return;
    const open = new Set();
    c.days.forEach(day => day.open.forEach(([a, b]) => { for (let h = Number(a.slice(0, 2)); h < Number(b.slice(0, 2)) + (b.slice(3) !== "00" ? 1 : 0); h++) open.add(h); }));
    config = { openHours: open }; load();
  });
  $("days").onchange = load;
  $("print").onclick = () => window.print();
  $("remindNow").onclick = async () => {
    const r = await fetch("/api/reminders/send-now", { method: "POST" });
    const d = await r.json().catch(() => ({}));
    $("remindStatus").textContent = !r.ok ? (d.detail || "Failed") : d.not_configured
      ? "Messaging isn't set up (add Twilio WhatsApp/SMS settings)." : `Sent ${d.sent}, failed ${d.failed}, no phone ${d.skipped_no_phone}.`;
  };
  load();
})();
