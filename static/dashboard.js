// Owner dashboard: KPIs, call log with transcripts, appointments.
(() => {
  const $ = (id) => document.getElementById(id);
  const LANG = { "en-IN": "English", "hi-IN": "Hindi", "te-IN": "Telugu" };
  const OUTCOME_CLASS = { booked: "ok", cancelled: "warn", transferred: "warn", failed: "bad", info: "", "in progress": "" };
  const PERIOD = { "1": "Today", "7": "Last 7 days", "30": "Last 30 days" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = (path, opts) => fetch(path, opts).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });

  function kpi(label, value) {
    return `<div class="panel kpi"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`;
  }

  async function loadStats() {
    const s = await api(`/api/stats?days=${$("range").value}`);
    const langs = Object.entries(s.by_language).map(([k, v]) => `${LANG[k] || k} ${v}`).join(" · ") || "–";
    const period = PERIOD[$("range").value] || "";
    $("period").textContent = `${period} · since ${new Date(s.since).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })}` +
      (s.failed_calls ? ` · ${s.failed_calls} failed connection${s.failed_calls > 1 ? "s" : ""} not counted` : "");
    $("kpis").innerHTML = [
      kpi("Calls answered", s.total_calls),
      kpi("Appointments booked", s.booked),
      kpi("Booking rate", `${Math.round(s.conversion_rate * 100)}%`),
      kpi("Avg response latency", s.avg_latency_ms ? `${s.avg_latency_ms} ms` : "–"),
      kpi("Transferred to staff", s.transferred),
      kpi("Upcoming appointments (all)", s.upcoming_appointments),
    ].join("") + `<div class="panel kpi"><div class="label">Languages</div><div class="small" style="margin-top:8px">${esc(langs)}</div></div>`;
  }

  async function loadCalls() {
    const calls = await api(`/api/calls?limit=50&days=${$("range").value}`);
    $("calls").innerHTML = calls.map(c => `
      <tr class="clickable" data-id="${esc(c.id)}">
        <td>${new Date(c.started_at).toLocaleString()}</td>
        <td>${esc(c.caller || (c.channel === "browser" ? "Web demo" : "Unknown"))}</td>
        <td>${esc(LANG[c.language] || c.language || "–")}</td>
        <td><span class="badge ${OUTCOME_CLASS[c.outcome || "in progress"] || ""}">${esc(c.outcome || c.status)}</span></td>
        <td>${c.avg_latency_ms ? esc(c.avg_latency_ms) + " ms" : "–"}</td>
        <td class="small">${esc(c.summary || "")}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="muted">No calls in this period — try the live demo.</td></tr>`;
    document.querySelectorAll("#calls tr.clickable").forEach(tr => tr.onclick = () => openCall(tr.dataset.id));
  }

  async function loadAppts() {
    const appts = await api("/api/appointments");
    $("appts").innerHTML = appts.map(a => `
      <tr>
        <td>${esc(a.start_local)}</td>
        <td>${esc(a.patient_name)}<div class="small muted">${esc(a.phone || "")}</div></td>
        <td>${esc(a.provider || "")}</td>
        <td>${a.status === "booked" ? `<button data-cancel="${esc(a.id)}">Cancel</button>` : `<span class="badge warn">${esc(a.status)}</span>`}</td>
      </tr>`).join("") || `<tr><td colspan="4" class="muted">No upcoming appointments.</td></tr>`;
    document.querySelectorAll("[data-cancel]").forEach(b => b.onclick = async () => {
      if (!confirm("Cancel this appointment?")) return;
      await api(`/api/appointments/${b.dataset.cancel}/cancel`, { method: "POST" });
      refresh();
    });
  }

  async function openCall(id) {
    const c = await api(`/api/calls/${id}`);
    $("dlgTitle").textContent = `${c.caller || c.channel} · ${new Date(c.started_at).toLocaleString()} · ${c.duration_s ?? "–"}s`;
    $("dlgBody").innerHTML = (c.summary ? `<div class="msg event">${esc(c.summary)}</div>` : "") + c.turns.map(t => {
      if (t.role === "tool") {
        const r = (t.meta && t.meta.result) || {};
        if (t.text === "search_clinic_knowledge") {
          const cites = (r.citations || []).map(c => `<span class="badge">📄 ${esc(c)}</span>`).join(" ") || "nothing found";
          return `<div class="msg event">🔎 Looked up “${esc((t.meta.args || {}).query)}” → ${cites}</div>`;
        }
        return `<div class="msg event">⚙ ${esc(t.text)} ${esc(JSON.stringify(t.meta))}</div>`;
      }
      const lat = t.latency_ms ? ` <span class="badge">${t.latency_ms} ms</span>` : "";
      return `<div class="msg ${t.role === "user" ? "user" : "assistant"}">${esc(t.text)}${lat}</div>`;
    }).join("");
    $("callDialog").showModal();
  }
  $("dlgClose").onclick = () => $("callDialog").close();

  async function refresh() {
    try { await Promise.all([loadStats(), loadCalls(), loadAppts()]); } catch (e) { console.error(e); }
  }
  fetch("/api/public-config").then(r => r.json()).then(c => $("bizName").textContent = c.business);
  $("range").onchange = refresh;
  refresh();
  setInterval(refresh, 15000);
})();
