// OPD reception screen: token queues per doctor and session, walk-in tokens, Next patient, delays.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${r.status}`);
    return data;
  };
  const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });
  const nice = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; };
  const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const STATUS = {
    waiting: ["Waiting", ""], in_consultation: ["With doctor", "ok"], done: ["Done", "muted-badge"],
    skipped: ["Missed turn", "warn"], cancelled: ["Cancelled", "bad"],
  };

  let day = new Date(); day.setHours(0, 0, 0, 0);
  let data = null;

  // ------------------------------------------------------------ rendering
  function tokenRow(t) {
    const [label, cls] = STATUS[t.status] || [t.status, ""];
    const actions = [];
    const unconfirmed = t.confirmed === false && ["waiting", "in_consultation"].includes(t.status);
    if (unconfirmed) actions.push(["confirm", "Confirm"], ["reject", "Reject"]);
    if (t.status === "waiting") actions.push(["in_consultation", "Call in"], ["skipped", "Missed"]);
    if (t.status === "in_consultation") actions.push(["done", "Done"]);
    if (t.status === "skipped" || t.status === "done") actions.push(["waiting", "Back to queue"]);
    if (t.status === "waiting" || t.status === "skipped") actions.push(["cancelled", "Cancel"]);
    const who = [t.age ? `${t.age} y` : "", t.reason || ""].filter(Boolean).join(" · ");
    const meta = [t.phone || "", `${t.source === "call" ? "AI call" : "Desk"} ${t.created_at}`].filter(Boolean).join(" · ");
    const free = t.fee_type === "follow_up" && !t.fee_amount;
    const fee = t.fee_type == null ? "" : free ? `<span class="badge ok">Free follow-up</span>`
      : t.paid ? `<span class="badge ok">${esc(data.currency)}${t.fee_amount} paid</span>`
      : `<span class="badge warn">${esc(data.currency)}${t.fee_amount}${t.fee_type === "follow_up" ? " follow-up" : ""} due</span>`;
    if (t.fee_type != null && !free && t.status !== "cancelled") actions.push([t.paid ? "unpaid" : "paid", t.paid ? "Undo paid" : "Mark paid"]);
    return `<tr class="${t.status === "in_consultation" ? "is-serving" : ""}${["done", "cancelled"].includes(t.status) ? " is-past" : ""}">
      <td class="tok">${t.number}</td>
      <td><b>${esc(t.patient_name)}</b>${who ? ` <span class="small muted">${esc(who)}</span>` : ""}<div class="small muted">${esc(meta)}</div></td>
      <td class="state"><span class="badge ${cls}">${label}</span> ${unconfirmed ? `<span class="badge warn" title="Pilot mode: the AI gave this token, staff haven't confirmed it yet">Not confirmed</span>` : ""} ${fee}
        <div class="actions">${actions.map(([s, l]) => `<button class="mini" data-token="${esc(t.id)}" data-status="${s}">${l}</button>`).join("")}</div></td>
    </tr>`;
  }

  const VISIT = { null: ["Expected", ""], arrived: ["Arrived", "ok"], done: ["Seen", "muted-badge"], no_show: ["No-show", "bad"] };
  function apptRow(a) {
    const [label, cls] = VISIT[a.visit] || VISIT.null;
    const pending = a.confirmed === false;
    const actions = [];
    if (pending) actions.push(["confirm", "Confirm"], ["reject", "Reject"]);
    if (!a.visit) actions.push(["arrived", "Arrived"], ["no_show", "No-show"]);
    if (a.visit === "arrived") actions.push(["done", "Seen"]);
    if (a.visit) actions.push(["", "Undo"]);
    const fee = a.fee_type == null ? "" : a.fee_type === "follow_up" && !a.fee_amount ? `<span class="badge ok">Free follow-up</span>`
      : `<span class="badge">${esc(data.currency)}${a.fee_amount}</span>`;
    return `<tr class="is-appt${a.visit === "done" || a.visit === "no_show" ? " is-past" : ""}">
      <td class="tok appt-time" title="Fixed-time appointment">${nice(a.time)}</td>
      <td><b>${esc(a.patient_name)}</b> <span class="badge appt-badge">Appointment</span>${a.reason ? ` <span class="small muted">${esc(a.reason)}</span>` : ""}
        <div class="small muted">${esc([a.phone || "", a.source === "call" ? "AI call" : "Desk"].filter(Boolean).join(" · "))}</div></td>
      <td class="state"><span class="badge ${cls}">${label}</span> ${pending ? `<span class="badge warn" title="Pilot mode: staff haven't confirmed this AI booking yet">Not confirmed</span>` : ""} ${fee}
        <div class="actions">${actions.map(([v, l]) => `<button class="mini" data-appt="${esc(a.id)}" data-visit="${v}">${l}</button>`).join("")}</div></td>
    </tr>`;
  }

  function sessionCard(doc, s) {
    const flags = [];
    if (s.cancelled) flags.push(`<span class="badge bad">Cancelled${s.note ? ": " + esc(s.note) : ""}</span>`);
    else if (s.delay_minutes) flags.push(`<span class="badge warn">Running ${s.delay_minutes} min late</span>`);
    if (s.ended) flags.push(`<span class="badge">Ended</span>`);
    else if (!s.accepting_tokens && !s.cancelled) flags.push(`<span class="badge">Phone tokens closed</span>`);
    const key = `data-doc="${esc(doc.id)}" data-session="${esc(s.session)}"`;
    return `<section class="panel opd-session${s.cancelled ? " is-cancelled" : ""}">
      <div class="opd-head">
        <div><h2>${esc(doc.name)} <span class="muted small">${esc(doc.department || doc.specialty)}</span></h2>
          <div class="small muted">${esc(s.label[0].toUpperCase() + s.label.slice(1))} OPD · ${nice(s.from)} – ${nice(s.to)} ${flags.join(" ")}</div></div>
        <div class="serving"><div class="small muted">Now serving</div><div class="num">${s.now_serving ?? "–"}</div></div>
      </div>
      <div class="opd-stats small"><span><b>${s.waiting}</b> waiting</span><span><b>${s.issued}</b> token${s.issued === 1 ? "" : "s"} issued</span><span><b>${s.appointments.length}</b> appointment${s.appointments.length === 1 ? "" : "s"}</span>${s.new_token_estimated_turn ? `<span>Next new token ≈ <b>${esc(s.new_token_estimated_turn)}</b></span>` : ""}</div>
      <div class="opd-actions">
        <button class="primary" data-act="next" ${key} ${s.waiting || s.now_serving ? "" : "disabled"}>Next patient</button>
        <button data-act="walkin" ${key} ${s.ended || s.cancelled ? "disabled" : ""}>+ Walk-in token</button>
        <button data-act="late" ${key}>${s.delay_minutes ? "Change delay" : "Doctor late"}</button>
        <button data-act="${s.cancelled ? "reopen" : "cancel"}" ${key} class="${s.cancelled ? "" : "danger-outline"}">${s.cancelled ? "Reopen OPD" : "Cancel OPD"}</button>
      </div>
      ${s.tokens.length || s.appointments.length ? `<div class="table-wrap"><table class="opd-table">
        <thead><tr><th>Token / time</th><th>Patient</th><th>Status</th></tr></thead>
        <tbody>${s.appointments.map(apptRow).join("")}${s.tokens.map(tokenRow).join("")}</tbody></table></div>`
        : `<p class="small muted">No tokens or appointments yet.</p>`}
    </section>`;
  }

  function render() {
    $("dayLabel").textContent = new Date(data.date + "T00:00").toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    $("updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    if (!data.enabled) { $("doctors").innerHTML = `<p class="panel muted">OPD tokens are switched off. Set <code>opd: enabled: true</code> in config/business.yaml.</p>`; return; }
    const cards = data.doctors.flatMap(doc => doc.sessions.map(s => sessionCard(doc, s)));
    $("doctors").innerHTML = cards.length ? cards.join("") : `<p class="panel muted">The clinic is closed on ${esc(data.weekday)}.</p>`;
  }

  async function load() {
    data = await api(`/api/opd?day=${isoDate(day)}`);
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
      try { const msg = await onSubmit(btn.dataset.action, new FormData($("dlgForm"))); if (msg) { err.textContent = msg; err.classList.add("ok-text"); setTimeout(() => dlg.close(), 1600); } else dlg.close(); await load(); }
      catch (ex) { if (err) { err.classList.remove("ok-text"); err.textContent = ex.message; } }
    }));
  }

  function walkIn(doc, session) {
    open(`Walk-in token · ${doc.name}`, `
      <div class="stack-form">
        <label class="field">Patient name <input name="patient_name" required autocomplete="off" /></label>
        <label class="field">Phone (for SMS and future visits) <input name="phone" type="tel" placeholder="+91…" /></label>
        <div class="tabs"><label class="field">Age <input name="age" type="number" min="0" max="120" /></label>
        <label class="field">Reason <input name="reason" placeholder="e.g. fever" /></label></div>
        <button class="primary" data-action="issue">Give token</button>
      </div><p class="err small"></p>`,
    async (_, f) => {
      if (f.get("patient_name").trim().length < 2) throw new Error("Enter the patient's name");
      const r = await post("/api/opd/tokens", { provider_id: doc.id, session, day: isoDate(day), patient_name: f.get("patient_name").trim(),
        phone: f.get("phone") || null, age: f.get("age") ? Number(f.get("age")) : null, reason: f.get("reason") || null });
      const fee = r.fee_type === "follow_up" && !r.fee_amount ? "free follow-up" : `fee ${data.currency}${r.fee_amount}${r.fee_type === "follow_up" ? " (follow-up)" : ""}`;
      return r.created ? `Token ${r.number} given, ${fee}.` : `This patient already has token ${r.number}.`;
    });
  }

  function late(doc, s) {
    open(`Doctor running late · ${doc.name}`, `
      <div class="stack-form">
        <label class="field">Delay <select name="delay">${[0, 15, 30, 45, 60, 90, 120].map(m => `<option value="${m}" ${m === s.delay_minutes ? "selected" : ""}>${m ? m + " minutes" : "On time"}</option>`).join("")}</select></label>
        <p class="small muted">VaaniDesk tells callers about the delay and adds it to every patient's estimated time.</p>
        <button class="primary" data-action="save">Save</button>
      </div><p class="err small"></p>`,
    async (_, f) => { await post("/api/opd/session-status", { provider_id: doc.id, day: isoDate(day), session: s.session, delay_minutes: Number(f.get("delay")) }); });
  }

  function cancelSession(doc, s) {
    open(`Cancel ${s.label} OPD · ${doc.name}`, `
      <div class="stack-form">
        <label class="field">Note for callers <input name="note" placeholder="e.g. Doctor on leave today" /></label>
        <p class="small muted">No new tokens will be given. Patients who already have a token should be informed by the desk.</p>
        <button class="danger" data-action="cancel">Cancel this OPD</button>
      </div><p class="err small"></p>`,
    async (_, f) => { await post("/api/opd/session-status", { provider_id: doc.id, day: isoDate(day), session: s.session, cancelled: true, note: f.get("note") || "" }); });
  }

  // ------------------------------------------------------------ events
  $("doctors").addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn || btn.disabled) return;
    try {
      if (btn.dataset.appt) {
        const v = btn.dataset.visit;
        if (v === "confirm" || v === "reject") {
          if (v === "reject" && !confirm("Reject and cancel this appointment? The patient is told to call the clinic.")) return;
          await post(`/api/pending/appointment/${btn.dataset.appt}`, { approve: v === "confirm" });
        } else await post(`/api/opd/appointments/${btn.dataset.appt}/visit`, { visit: v || null });
        return load();
      }
      if (btn.dataset.token) {
        if (["confirm", "reject"].includes(btn.dataset.status)) {
          if (btn.dataset.status === "reject" && !confirm("Reject this token? The patient is told to call the clinic.")) return;
          await post(`/api/pending/token/${btn.dataset.token}`, { approve: btn.dataset.status === "confirm" });
          return load();
        }
        if (btn.dataset.status === "cancelled" && !confirm("Cancel this token?")) return;
        if (["paid", "unpaid"].includes(btn.dataset.status)) await post(`/api/opd/tokens/${btn.dataset.token}/paid`, { paid: btn.dataset.status === "paid" });
        else await post(`/api/opd/tokens/${btn.dataset.token}/status`, { status: btn.dataset.status });
        return load();
      }
      const doc = data.doctors.find(d => d.id === btn.dataset.doc);
      const s = doc && doc.sessions.find(x => x.session === btn.dataset.session);
      if (!s) return;
      if (btn.dataset.act === "next") { btn.disabled = true; await post("/api/opd/next", { provider_id: doc.id, session: s.session, day: isoDate(day) }); return load(); }
      if (btn.dataset.act === "walkin") return walkIn(doc, s.session);
      if (btn.dataset.act === "late") return late(doc, s);
      if (btn.dataset.act === "cancel") return cancelSession(doc, s);
      if (btn.dataset.act === "reopen") { await post("/api/opd/session-status", { provider_id: doc.id, day: isoDate(day), session: s.session, cancelled: false, note: "" }); return load(); }
    } catch (ex) { alert(ex.message); }
  });

  const shift = (n) => { day.setDate(day.getDate() + n); load(); };
  $("prev").onclick = () => shift(-1);
  $("next").onclick = () => shift(1);
  $("today").onclick = () => { day = new Date(); day.setHours(0, 0, 0, 0); load(); };
  load().catch(e => ($("doctors").innerHTML = `<p class="muted">Could not load the OPD queue: ${esc(e.message)}</p>`));
  setInterval(() => { if (!dlg.open && !document.hidden) load().catch(() => {}); }, 10000);
})();
