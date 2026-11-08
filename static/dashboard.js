// Owner dashboard: KPIs, call log with transcripts, appointments.
(() => {
  const $ = (id) => document.getElementById(id);
  const LANG = { "en-IN": "English", "hi-IN": "Hindi", "te-IN": "Telugu" };
  const OUTCOME_CLASS = { booked: "ok", token: "ok", cancelled: "warn", transferred: "warn", failed: "bad", info: "", "in progress": "" };
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
      kpi("OPD tokens by phone", s.opd_tokens ?? 0),
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
        <td class="nowrap">${a.status === "booked" ? `<button class="mini" data-msg="${esc(a.id)}">Message</button> <button class="mini" data-cancel="${esc(a.id)}">Cancel</button>` : `<span class="badge warn">${esc(a.status)}</span>`}</td>
      </tr>`).join("") || `<tr><td colspan="4" class="muted">No upcoming appointments.</td></tr>`;
    document.querySelectorAll("[data-msg]").forEach(b => b.onclick = () => {
      const a = appts.find(x => x.id === b.dataset.msg);
      openCompose({ phone: a.phone, email: a.email, who: `${a.patient_name} · ${a.start_local} · ${a.provider}` });
    });
    document.querySelectorAll("[data-cancel]").forEach(b => b.onclick = async () => {
      if (!confirm("Cancel this appointment?")) return;
      await api(`/api/appointments/${b.dataset.cancel}/cancel`, { method: "POST" });
      refresh();
    });
  }

  const KIND = { outage: "AI failed mid-call", abandoned: "Call ended before booking", callback: "Asked for a call back",
                 transfer_failed: "Transfer failed", rejected: "Booking rejected" };
  async function loadAttention() {
    const a = await api("/api/attention");
    const n = a.pending.length + a.follow_ups.length;
    $("attention").hidden = n === 0;
    $("attnCount").textContent = n || "";
    $("pendingWrap").hidden = !a.pending.length;
    $("followWrap").hidden = !a.follow_ups.length;
    $("pending").innerHTML = a.pending.map(p => `
      <tr><td>${esc(p.patient_name)}<div class="small muted">${esc(p.phone || "")}</div></td>
        <td>${esc(p.doctor)}</td><td>${esc(p.when)}${p.kind === "token" ? ` <span class="badge">OPD</span>` : ""}</td>
        <td class="nowrap"><button class="primary" data-decide="${esc(p.kind)}/${esc(p.id)}" data-ok="1">Confirm</button>
          <button data-decide="${esc(p.kind)}/${esc(p.id)}" data-ok="0">Reject</button></td></tr>`).join("");
    $("follow").innerHTML = a.follow_ups.map(f => `
      <tr><td class="small">${esc(f.at)}</td>
        <td>${esc(f.patient_name || "–")}<div class="small muted">${esc(f.phone || "number unknown")}</div></td>
        <td><b>${esc(KIND[f.kind] || f.kind)}</b><div class="small muted">${esc(f.reason)}</div>
          ${f.call_id ? `<a href="#" class="small" data-call="${esc(f.call_id)}">Open call</a>` : ""}</td>
        <td><button data-done="${esc(f.id)}">Done</button></td></tr>`).join("");
    document.querySelectorAll("[data-decide]").forEach(b => b.onclick = async () => {
      const ok = b.dataset.ok === "1";
      if (!ok && !confirm("Reject and cancel this booking? The patient gets an SMS asking them to call.")) return;
      await api(`/api/pending/${b.dataset.decide}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approve: ok }) });
      refresh();
    });
    document.querySelectorAll("[data-done]").forEach(b => b.onclick = async () => {
      const note = prompt("What did you do? (optional)") ?? null;
      await api(`/api/follow-ups/${b.dataset.done}/done`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note }) });
      refresh();
    });
    document.querySelectorAll("#follow [data-call]").forEach(l => l.onclick = (e) => { e.preventDefault(); openCall(l.dataset.call); });
  }

  // ---- messages to patients
  const VIA = { whatsapp: "WhatsApp", sms: "SMS", email: "Email", none: "—" };
  const MSTATUS = { sent: ["Sent", "ok"], failed: ["Failed", "bad"], not_configured: ["Not set up", "warn"] };
  let channels = {};
  async function loadMessages() {
    const d = await api("/api/messages?limit=30");
    channels = d.channels;
    $("channels").innerHTML = Object.entries(d.channels).map(([k, on]) => `<span class="badge ${on ? "ok" : "off"}" title="${on ? "Set up" : "Not set up yet: see docs/GO_LIVE.md"}">${VIA[k]} ${on ? "✓" : "off"}</span>`).join(" ");
    $("msgs").innerHTML = d.messages.map(m => {
      const [label, cls] = MSTATUS[m.status] || [m.status, ""];
      return `<tr><td class="small nowrap">${esc(m.at)}</td><td class="small">${esc(m.to)}</td><td class="small">${esc(VIA[m.channel] || m.channel)}</td>
        <td class="small">${esc(m.preview.slice(0, 140))}</td><td><span class="badge ${cls}" title="${esc(m.error || "")}">${label}</span></td></tr>`;
    }).join("") || `<tr><td colspan="5" class="muted">No messages yet. Booking confirmations, reminders and your messages appear here.</td></tr>`;
  }
  const QUICK = ["The doctor is running about 30 minutes late today. Sorry for the wait.",
                 "Please bring your previous prescriptions and reports to your visit.",
                 "Your reports are ready. You can collect them at the reception."];
  function openCompose({ phone = "", email = "", who = "" } = {}) {
    const f = $("msgForm");
    f.reset(); f.phone.value = phone || ""; f.email.value = email || "";
    $("msgFor").textContent = who; $("msgErr").textContent = "";
    const phoneVia = channels.whatsapp ? "WhatsApp (SMS if that fails)" : channels.sms ? "SMS" : null;
    $("msgVia").textContent = `Sent by ${[phoneVia, channels.email ? "email" : null].filter(Boolean).join(" and ") || "— no messaging set up yet (see docs/GO_LIVE.md)"}.`;
    f.querySelector(".msg-quick").innerHTML = QUICK.map(q => `<button type="button" class="chip-sm">${esc(q)}</button>`).join("");
    f.querySelectorAll(".chip-sm").forEach(c => c.onclick = () => { f.text.value = c.textContent; });
    $("msgDialog").showModal();
  }
  $("newMsg").onclick = () => openCompose();
  $("msgForm").querySelector("[data-send]").onclick = async (e) => {
    e.preventDefault();
    const f = $("msgForm"), btn = e.target;
    btn.disabled = true;
    try {
      const r = await fetch("/api/messages", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone: f.phone.value, email: f.email.value, subject: f.subject.value, text: f.text.value }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Please fill in the message and a phone or email.");
      $("msgErr").textContent = d.sent.length ? "" : "Not sent: " + (d.failed.length ? "the provider rejected it (see the message log)." : "no messaging is set up yet.");
      if (d.sent.length) $("msgDialog").close();
      loadMessages();
    } catch (ex) { $("msgErr").textContent = ex.message; }
    btn.disabled = false;
  };

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
    try { await Promise.all([loadAttention(), loadStats(), loadCalls(), loadAppts(), loadMessages()]); } catch (e) { console.error(e); }
  }
  $("range").onchange = refresh;
  refresh();
  setInterval(refresh, 15000);
})();
