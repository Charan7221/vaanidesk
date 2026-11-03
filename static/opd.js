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
    if (t.status === "waiting") actions.push(["in_consultation", "Call in"], ["skipped", "Missed"]);
    if (t.status === "in_consultation") actions.push(["done", "Done"]);
    if (t.status === "skipped" || t.status === "done") actions.push(["waiting", "Back to queue"]);
    if (t.status === "waiting" || t.status === "skipped") actions.push(["cancelled", "Cancel"]);
    const who = [t.age ? `${t.age} y` : "", t.reason || ""].filter(Boolean).join(" · ");
    const meta = [t.phone || "", `${t.source === "call" ? "AI call" : "Desk"} ${t.created_at}`].filter(Boolean).join(" · ");
    return `<tr class="${t.status === "in_consultation" ? "is-serving" : ""}${["done", "cancelled"].includes(t.status) ? " is-past" : ""}">
      <td class="tok">${t.number}</td>
      <td><b>${esc(t.patient_name)}</b>${who ? ` <span class="small muted">${esc(who)}</span>` : ""}<div class="small muted">${esc(meta)}</div></td>
      <td class="state"><span class="badge ${cls}">${label}</span>
        <div class="actions">${actions.map(([s, l]) => `<button class="mini" data-token="${esc(t.id)}" data-status="${s}">${l}</button>`).join("")}</div></td>
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
        <div><h2>${esc(doc.name)} <span class="muted small">${esc(doc.specialty)}</span></h2>
          <div class="small muted">${esc(s.label[0].toUpperCase() + s.label.slice(1))} OPD · ${nice(s.from)} – ${nice(s.to)} ${flags.join(" ")}</div></div>
        <div class="serving"><div class="small muted">Now serving</div><div class="num">${s.now_serving ?? "–"}</div></div>
      </div>
      <div class="opd-stats small"><span><b>${s.waiting}</b> waiting</span><span><b>${s.issued}</b> token${s.issued === 1 ? "" : "s"} issued</span>${s.new_token_estimated_turn ? `<span>Next new token ≈ <b>${esc(s.new_token_estimated_turn)}</b></span>` : ""}</div>
      <div class="opd-actions">
        <button class="primary" data-act="next" ${key} ${s.waiting || s.now_serving ? "" : "disabled"}>Next patient</button>
        <button data-act="walkin" ${key} ${s.ended || s.cancelled ? "disabled" : ""}>+ Walk-in token</button>
        <button data-act="late" ${key}>${s.delay_minutes ? "Change delay" : "Doctor late"}</button>
        <button data-act="${s.cancelled ? "reopen" : "cancel"}" ${key} class="${s.cancelled ? "" : "danger-outline"}">${s.cancelled ? "Reopen OPD" : "Cancel OPD"}</button>
      </div>
      ${s.tokens.length ? `<div class="table-wrap"><table class="opd-table">
        <thead><tr><th>Token</th><th>Patient</th><th>Status</th></tr></thead>
        <tbody>${s.tokens.map(tokenRow).join("")}</tbody></table></div>`
        : `<p class="small muted">No tokens yet.</p>`}
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
      return r.created ? `Token ${r.number} given.` : `This patient already has token ${r.number}.`;
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
      if (btn.dataset.token) {
        if (btn.dataset.status === "cancelled" && !confirm("Cancel this token?")) return;
        await post(`/api/opd/tokens/${btn.dataset.token}/status`, { status: btn.dataset.status });
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

  fetch("/api/public-config").then(r => r.json()).then(c => ($("bizName").textContent = c.business));
  load().catch(e => ($("doctors").innerHTML = `<p class="muted">Could not load the OPD queue: ${esc(e.message)}</p>`));
  setInterval(() => { if (!dlg.open && !document.hidden) load().catch(() => {}); }, 10000);
})();
