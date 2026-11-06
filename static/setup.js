// Clinic setup: edit the clinic profile (doctors, hours, fees, departments, FAQ, settings) and staff accounts.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const api = async (path, opts = {}) => {
    const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${r.status}`);
    return data;
  };
  const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  const DAY_NAMES = { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" };
  const LANGS = { "en-IN": "English", "hi-IN": "Hindi", "te-IN": "Telugu" };
  let cfg = null;

  const field = (label, name, value, type = "text", extra = "") =>
    `<label class="field">${label}<input name="${name}" type="${type}" value="${esc(value ?? "")}" ${extra} /></label>`;
  const check = (label, name, on) => `<label class="check"><input type="checkbox" name="${name}" ${on ? "checked" : ""} /> ${label}</label>`;
  const num = (v) => (v === "" || v == null ? null : Number(v));

  function render() {
    $("clinic").innerHTML = [
      field("Clinic name", "name", cfg.name), field("Assistant's name", "assistant_name", cfg.assistant_name),
      field("Address", "address", cfg.address), field("Clinic phone", "phone", cfg.phone),
      field("Transfer calls to (staff number)", "handoff_number", cfg.handoff_number),
      field("Emergency / ambulance number", "emergency_number", cfg.emergency_number),
      `<div class="field">Languages<div>${Object.entries(LANGS).map(([k, v]) => check(v, "lang_" + k, cfg.languages.includes(k))).join(" ")}</div></div>`,
      `<label class="field">Greeting<select name="multilingual_greeting">${["full", "short", "off"].map(m => `<option ${cfg.multilingual_greeting === m ? "selected" : ""}>${m}</option>`).join("")}</select></label>`,
    ].join("");
    $("hours").innerHTML = DAYS.map(d => field(DAY_NAMES[d], "hours_" + d, (cfg.hours[d] || []).map(w => w.join("-")).join(", "), "text", 'placeholder="closed"')).join("");
    renderDoctors(); renderDepts(); renderFaq();
    const f = cfg.fees;
    $("fees").innerHTML = [field("Currency", "currency", f.currency), field("Default consultation fee", "consultation_fee", f.consultation_fee, "number", 'min="0"'),
      field("Follow-up window (days)", "follow_up_days", f.follow_up_days, "number", 'min="0"'), field("Follow-up fee (0 = free)", "follow_up_fee", f.follow_up_fee, "number", 'min="0"')].join("");
    $("texts").innerHTML = `
      <label class="field">Services and prices (one per line: <code>name | price</code>)<textarea name="services" rows="5">${esc(cfg.services.map(s => `${s.name} | ${s.price}`).join("\n"))}</textarea></label>
      <label class="field">Policies for the assistant (one per line)<textarea name="policies" rows="5">${esc(cfg.policies.join("\n"))}</textarea></label>`;
    const o = cfg.opd;
    $("settings").innerHTML = [
      check("<b>Pilot mode</b>: AI bookings wait for staff confirmation", "pilot_mode", cfg.pilot_mode),
      check("Tell callers they're talking to an AI assistant", "consent_notice", cfg.consent_notice),
      field("Delete call transcripts after (days, 0 = keep)", "transcript_retention_days", cfg.transcript_retention_days, "number", 'min="0"'),
      field("SMS alerts for follow-ups to", "staff_alert_phone", cfg.staff_alert_phone, "tel", 'placeholder="defaults to transfer number"'),
      field("Appointment length (minutes)", "slot_minutes", cfg.slot_minutes, "number", 'min="5"'),
      field("Book up to (days ahead)", "booking_horizon_days", cfg.booking_horizon_days, "number", 'min="1"'),
      check("Day-before reminders (WhatsApp/SMS)", "rem_enabled", (cfg.reminders || {}).enabled !== false),
      field("Send reminders at", "rem_at", (cfg.reminders || {}).send_at || "18:00", "time"),
      check("OPD walk-in tokens", "opd_enabled", o.enabled),
      field("Average consultation (minutes)", "opd_avg", o.avg_consult_minutes, "number", 'min="1"'),
      field("Max tokens per doctor per session", "opd_max", o.max_tokens_per_session, "number", 'min="1"'),
      field("Phone tokens stop before session end (minutes)", "opd_stop", o.stop_before_end_minutes, "number", 'min="0"'),
      field("Tokens up to (days ahead)", "opd_advance", o.advance_days, "number", 'min="0"'),
    ].join("");
  }

  function deptOptions(sel) {
    return `<option value="">—</option>` + cfg.departments.map(d => `<option value="${esc(d.id)}" ${d.id === sel ? "selected" : ""}>${esc(d.name)}</option>`).join("");
  }
  function renderDoctors() {
    $("doctors").innerHTML = cfg.providers.map((p, i) => `<tr data-i="${i}">
      <td><input data-k="id" value="${esc(p.id)}" size="10" /></td><td><input data-k="name" value="${esc(p.name)}" /></td>
      <td><input data-k="specialty" value="${esc(p.specialty)}" /></td><td><select data-k="department">${deptOptions(p.department)}</select></td>
      <td><input data-k="consultation_fee" type="number" min="0" value="${esc(p.consultation_fee ?? "")}" size="6" placeholder="dept." /></td>
      <td><button class="mini" data-del="providers" data-i="${i}">Remove</button></td></tr>`).join("");
  }
  function renderDepts() {
    $("depts").innerHTML = cfg.departments.map((d, i) => `<fieldset class="stack dept" data-i="${i}">
      <legend>${esc(d.name || "New department")}</legend>
      <div class="grid3">${field("Id", "id", d.id)}${field("Name", "name", d.name)}
        ${field("Fee", "consultation_fee", d.consultation_fee, "number", 'min="0" placeholder="default"')}
        ${field("Min age", "age_min", d.age_min, "number", 'min="0"')}${field("Max age", "age_max", d.age_max, "number", 'min="0"')}
        <label class="field">Only for<select name="gender"><option value="">anyone</option><option value="female" ${d.gender === "female" ? "selected" : ""}>women</option></select></label>
        ${field("Follow-up days", "follow_up_days", d.follow_up_days, "number", 'min="0" placeholder="default"')}
        ${field("Follow-up fee", "follow_up_fee", d.follow_up_fee, "number", 'min="0" placeholder="default"')}
        ${check("Default (unmatched problems)", "default", d.default)}</div>
      <label class="field">Treats<textarea name="treats" rows="2">${esc(d.treats.join(", "))}</textarea></label>
      <button class="mini" data-del="departments" data-i="${i}">Remove department</button></fieldset>`).join("");
  }
  function renderFaq() {
    $("faq").innerHTML = cfg.faq.map((f, i) => `<div class="faq-row" data-i="${i}">
      <input data-k="q" value="${esc(f.q)}" placeholder="Question" /><input data-k="a" value="${esc(f.a)}" placeholder="Answer" />
      <button class="mini" data-del="faq" data-i="${i}">Remove</button></div>`).join("");
  }

  // read the forms back into cfg
  function collect() {
    const v = (name) => document.querySelector(`[name="${name}"]`);
    ["name", "assistant_name", "address", "phone", "handoff_number", "emergency_number", "staff_alert_phone"].forEach(k => cfg[k] = v(k).value.trim());
    cfg.multilingual_greeting = v("multilingual_greeting").value;
    cfg.languages = Object.keys(LANGS).filter(k => v("lang_" + k).checked);
    DAYS.forEach(d => {
      const raw = v("hours_" + d).value.trim();
      cfg.hours[d] = raw ? raw.split(",").map(r => r.trim().split("-").map(t => t.trim().padStart(5, "0"))) : [];
    });
    cfg.providers = [...$("doctors").querySelectorAll("tr")].map(tr => {
      const g = (k) => tr.querySelector(`[data-k="${k}"]`).value.trim();
      return { id: g("id"), name: g("name"), specialty: g("specialty"), department: g("department") || null, consultation_fee: num(g("consultation_fee")) };
    });
    cfg.departments = [...document.querySelectorAll(".dept")].map(fs => {
      const g = (k) => fs.querySelector(`[name="${k}"]`);
      return { id: g("id").value.trim(), name: g("name").value.trim(), treats: g("treats").value.split(",").map(t => t.trim()).filter(Boolean),
        age_min: num(g("age_min").value), age_max: num(g("age_max").value), gender: g("gender").value || null, default: g("default").checked,
        consultation_fee: num(g("consultation_fee").value), follow_up_days: num(g("follow_up_days").value), follow_up_fee: num(g("follow_up_fee").value) };
    });
    cfg.faq = [...$("faq").querySelectorAll(".faq-row")].map(r => ({ q: r.querySelector('[data-k="q"]').value.trim(), a: r.querySelector('[data-k="a"]').value.trim() })).filter(f => f.q && f.a);
    cfg.fees = { currency: v("currency").value.trim() || "₹", consultation_fee: num(v("consultation_fee").value) ?? 0,
      follow_up_days: num(v("follow_up_days").value) ?? 7, follow_up_fee: num(v("follow_up_fee").value) ?? 0 };
    cfg.services = v("services").value.split("\n").map(l => l.split("|")).filter(p => p[0].trim()).map(([n, p]) => ({ name: n.trim(), price: (p || "").trim() }));
    cfg.policies = v("policies").value.split("\n").map(l => l.trim()).filter(Boolean);
    cfg.pilot_mode = v("pilot_mode").checked; cfg.consent_notice = v("consent_notice").checked;
    cfg.transcript_retention_days = num(v("transcript_retention_days").value) ?? 90;
    cfg.slot_minutes = num(v("slot_minutes").value) ?? 20; cfg.booking_horizon_days = num(v("booking_horizon_days").value) ?? 14;
    cfg.reminders = { enabled: v("rem_enabled").checked, send_at: v("rem_at").value || "18:00" };
    cfg.opd = { ...cfg.opd, enabled: v("opd_enabled").checked, avg_consult_minutes: num(v("opd_avg").value) ?? 10,
      max_tokens_per_session: num(v("opd_max").value) ?? 40, stop_before_end_minutes: num(v("opd_stop").value) ?? 30, advance_days: num(v("opd_advance").value) ?? 2 };
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-del]");
    if (!b) return;
    e.preventDefault(); collect(); cfg[b.dataset.del].splice(Number(b.dataset.i), 1); render();
  });
  $("addDoctor").onclick = () => { collect(); cfg.providers.push({ id: "", name: "", specialty: "", department: null, consultation_fee: null }); render(); };
  $("addDept").onclick = () => { collect(); cfg.departments.push({ id: "", name: "", treats: [], default: false }); render(); };
  $("addFaq").onclick = () => { collect(); cfg.faq.push({ q: "", a: "" }); render(); };
  $("save").onclick = async () => {
    collect();
    $("status").className = "small"; $("status").textContent = "Saving…";
    try { await api("/api/setup", { method: "PUT", body: JSON.stringify({ config: cfg }) }); $("status").textContent = "Saved. New calls use these settings now."; $("status").classList.add("ok-text"); $("fileNote").hidden = true; }
    catch (ex) { $("status").textContent = ex.message; $("status").classList.add("err"); }
  };

  // ---- staff + audit
  async function loadStaff() {
    const staff = await api("/api/staff");
    $("staff").innerHTML = staff.map(u => `<tr><td>${esc(u.email)}${u.name ? `<div class="small muted">${esc(u.name)}</div>` : ""}</td>
      <td>${u.role === "owner" ? "Owner" : "Receptionist"}${u.active ? "" : ` <span class="badge bad">disabled</span>`}</td>
      <td class="small">${u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}</td>
      <td class="nowrap"><button class="mini" data-reset="${esc(u.id)}">Reset password</button>
        <button class="mini" data-active="${esc(u.id)}" data-to="${u.active ? 0 : 1}">${u.active ? "Disable" : "Enable"}</button></td></tr>`).join("") || `<tr><td colspan="4" class="muted">No staff accounts yet.</td></tr>`;
    $("staff").querySelectorAll("[data-reset]").forEach(b => b.onclick = async () => {
      const pw = prompt("New password (10+ characters):"); if (!pw) return;
      try { await api(`/api/staff/${b.dataset.reset}`, { method: "POST", body: JSON.stringify({ password: pw }) }); alert("Password changed."); } catch (ex) { alert(ex.message); }
    });
    $("staff").querySelectorAll("[data-active]").forEach(b => b.onclick = async () => {
      try { await api(`/api/staff/${b.dataset.active}`, { method: "POST", body: JSON.stringify({ active: b.dataset.to === "1" }) }); loadStaff(); } catch (ex) { alert(ex.message); }
    });
  }
  $("staffForm").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try { await api("/api/staff", { method: "POST", body: JSON.stringify(Object.fromEntries(f)) }); e.target.reset(); $("staffErr").textContent = ""; loadStaff(); loadAudit(); }
    catch (ex) { $("staffErr").textContent = ex.message; }
  };
  async function loadAudit() {
    const rows = await api("/api/audit?limit=50");
    $("audit").innerHTML = rows.map(r => `<tr><td class="small nowrap">${esc(r.at)}</td><td class="small">${esc(r.actor)}</td>
      <td>${esc(r.action)} ${r.target ? `<span class="muted small">${esc(r.target)}</span>` : ""}</td>
      <td class="small muted">${esc(r.detail ? JSON.stringify(r.detail) : "")}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">Nothing yet.</td></tr>`;
  }

  api("/api/setup").then(d => {
    cfg = d.config; render();
    if (d.config_file) { $("fileNote").hidden = false; $("fileName").textContent = d.config_file.split(/[\\/]/).pop(); }
  }).catch(ex => { document.querySelector("main").innerHTML = `<p class="panel">${esc(ex.message)}</p>`; });
  loadStaff().catch(() => {}); loadAudit().catch(() => {});
})();
