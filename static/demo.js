// VaaniDesk public chat + voice client: chat by typing or talk by voice; actions appear as cards.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let CLINIC = new URLSearchParams(location.search).get("clinic") || "";
  const msgs = $("messages"), body = $("chatBody"), input = $("textInput"), sendBtn = $("sendBtn");
  const CHIPS = {
    "en-IN": ["Book an appointment tomorrow evening", "I need an OPD token for today", "My child has a fever, which doctor?", "Do I need to fast before a sugar test?"],
    "hi-IN": ["कल शाम डॉक्टर से मिलना है", "आज का OPD टोकन चाहिए", "क्या आप कैशलेस इंश्योरेंस लेते हैं?"],
    "te-IN": ["రేపు సాయంత్రం అపాయింట్‌మెంట్ కావాలి", "ఈరోజు OPD టోకెన్ కావాలి", "షుగర్ టెస్ట్ ముందు ఏమీ తినకూడదా?"],
  };

  let cfg = { assistant: "Vaani", business: "", languages: ["en-IN", "hi-IN", "te-IN"] };
  let ws = null, mode = null, connecting = null, inCall = false, voiceReplies = true;
  let micCtx = null, micStream = null, micNode = null, playCtx = null, nextPlayTime = 0, active = [];
  let userBubble = null, botBubble = null, typingRow = null, lastRole = null, typedPending = null, greetedResolve = null;
  let consumed = 0;  // characters of the current reply already shown above an action card

  const lang = () => document.querySelector('input[name="lang"]:checked').value;
  const now = () => new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const scroll = () => { body.scrollTop = body.scrollHeight; };

  // ------------------------------------------------------------ config
  fetch(`/api/public-config${CLINIC ? "?clinic=" + encodeURIComponent(CLINIC) : ""}`).then(r => r.ok ? r.json() : Promise.reject())
    .then(c => {
      cfg = c;
      CLINIC = c.clinic;  // the server picks the clinic when the link doesn't name one
      const initial = (c.assistant || "V")[0].toUpperCase();
      document.title = `${c.assistant} · ${c.business}`;
      $("clinicName").textContent = c.business;
      $("agentName").textContent = $("headName").textContent = c.assistant;
      $("agentRole").textContent = `AI receptionist · ${c.business}`;
      document.querySelectorAll(".mini-avatar").forEach(a => (a.textContent = initial));
      $("welcomeTitle").textContent = `Hi! I'm ${c.assistant} from ${c.business}.`;
      if (c.emergency_number) $("emergency").textContent = c.emergency_number;
      if (c.access_code_required) $("codeField").hidden = false;
      if (c.phone_number) $("phoneLine").textContent = `Or call ${c.phone_number}`;
      document.querySelectorAll('input[name="lang"]').forEach(r => { r.closest("label").hidden = !c.languages.includes(r.value); });
      renderChips();
    }).catch(() => { $("welcomeText").textContent = "This clinic link isn't valid."; });

  const VIA = { whatsapp: "WhatsApp", sms: "SMS", email: "email" };
  function sentVia(r) {
    const ch = r.confirmation_channels || [];
    return ch.length ? "Sending by " + ch.map(c => VIA[c] || c).join(" + ") : null;
  }
  function renderChips() {
    $("chips").innerHTML = (CHIPS[lang()] || CHIPS["en-IN"]).map(t => `<button class="chip" type="button">${esc(t)}</button>`).join("");
  }
  $("chips").onclick = (e) => { const b = e.target.closest(".chip"); if (b) sendText(b.textContent); };
  document.querySelectorAll('input[name="lang"]').forEach(r => r.onchange = renderChips);

  // ------------------------------------------------------------ state display
  function setState(state, text) {
    const wrap = $("orbWrap");
    wrap.className = "orb-wrap " + state;
    $("stateText").textContent = text;
    $("statePill").classList.toggle("live", state !== "idle");
    $("headSub").textContent = state === "idle" ? "Typically replies in about a second" : text;
  }
  setState("idle", "Ready to help");

  // ------------------------------------------------------------ messages
  function row(role, html, cls = "") {
    $("welcome").hidden = true;
    const cont = lastRole === role;
    lastRole = role;
    const r = document.createElement("div");
    r.className = `row ${role}${cont ? " cont" : ""}`;
    r.innerHTML = (role === "assistant" ? `<span class="mini-avatar">${esc((cfg.assistant || "V")[0])}</span>` : "") +
      `<div class="bubble ${cls}">${html}</div>`;
    msgs.appendChild(r);
    scroll();
    return r.querySelector(".bubble");
  }
  function setBubble(el, text, final) {
    el.innerHTML = esc(text) + (final ? `<span class="time">${now()}</span>` : "");
    el.classList.toggle("partial", !final);
    scroll();
  }
  function extra(html, cls) {
    $("welcome").hidden = true;
    const d = document.createElement("div");
    d.className = cls;
    d.innerHTML = html;
    msgs.appendChild(d);
    lastRole = null;
    scroll();
    return d;
  }
  const note = (text, bad = false) => extra(esc(text), "note" + (bad ? " bad" : ""));
  function showTyping() {
    if (typingRow) return;
    typingRow = document.createElement("div");
    typingRow.className = "row assistant";
    typingRow.innerHTML = `<span class="mini-avatar">${esc((cfg.assistant || "V")[0])}</span><div class="bubble typing"><i></i><i></i><i></i></div>`;
    msgs.appendChild(typingRow);
    setState(inCall && mode === "voice" ? "thinking" : "thinking", "Thinking…");
    scroll();
  }
  function hideTyping() { if (typingRow) { typingRow.remove(); typingRow = null; } }

  // ------------------------------------------------------------ cards for the receptionist's actions
  const card = (kind, icon, title, inner) => extra(`<div class="card-head"><span class="ic">${icon}</span>${title}</div><div class="card-body">${inner}</div>`, `card ${kind}`);
  const kv = (pairs) => `<dl class="kv2">${pairs.filter(p => p[1] != null && p[1] !== "").map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
  function slotsHtml(times, doctor) {
    return `<div class="slots">${times.map(t => `<button class="slot" type="button" data-say="${esc(`${niceTime(t)}${doctor ? " with " + doctor : ""}, please`)}">${esc(niceTime(t))}</button>`).join("")}</div>`;
  }
  function niceTime(t) { const [h, m] = t.split(":").map(Number); return `${((h + 11) % 12) + 1}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`; }
  msgs.addEventListener("click", (e) => { const b = e.target.closest("[data-say]"); if (b) sendText(b.dataset.say); });

  function onTool(ev) {
    const a = ev.args || {}, r = ev.result || {};
    extra(esc(`⚙ ${ev.name}(${JSON.stringify(a)})\n→ ${JSON.stringify(r)}`), "step");
    switch (ev.name) {
      case "check_availability": {
        if (r.doctors) {
          const free = r.doctors.filter(d => d.available && d.available.length);
          if (free.length) card("", "🗓️", `Free times · ${r.weekday} ${r.date.slice(8)}/${r.date.slice(5, 7)}`,
            free.map(d => `<div><b>${esc(d.provider)}</b> <span class="muted small">${esc(d.specialty || "")}</span>${slotsHtml(d.available.slice(0, 6), d.provider)}</div>`).join(""));
        } else if (r.available && r.available.length) {
          card("", "🗓️", `${esc(r.provider || "Free times")} · ${esc(r.weekday)}`, slotsHtml(r.available.slice(0, 8), r.provider));
        }
        break;
      }
      case "book_appointment":
        if (r.booked) card(r.pending_confirmation ? "pending" : "ok", r.pending_confirmation ? "⏳" : "✓",
          r.pending_confirmation ? "Appointment requested · the clinic will confirm" : (r.updated ? "Appointment updated" : "Appointment booked"),
          kv([["Doctor", r.provider], ["When", r.when], ["Fee", r.fee], ["Reference", r.appointment_id], ["Confirmation", sentVia(r)]]));
        break;
      case "reschedule_appointment":
        if (r.rescheduled) card("ok", "↻", "Appointment moved", kv([["Doctor", r.provider], ["New time", r.when], ["Reference", r.appointment_id]]));
        break;
      case "cancel_appointment":
        if (r.cancelled) card("", "✕", "Appointment cancelled", kv([["Reference", r.appointment_id]]));
        break;
      case "get_opd_token":
        if (r.issued) card(r.pending_confirmation ? "pending" : "ok", "🎫", r.already_had_token ? "You already have this token" : (r.pending_confirmation ? "OPD token requested" : "OPD token"),
          `<div class="token-big"><b>${esc(r.token_number)}</b><div>${esc(r.doctor)}<div class="muted small">${esc(r.weekday)} · ${esc(r.session)}</div></div></div>` +
          kv([["Please arrive by", r.please_arrive_by], ["Estimated turn", r.estimated_turn], ["Patients ahead", r.patients_ahead],
              ["Fee", r.fee], ["Patient no.", r.patient_number], ["Confirmation", sentVia(r)]]));
        break;
      case "check_opd_status": {
        const t = (r.your_tokens || [])[0];
        if (t) card("", "🎫", `Your token ${t.token_number} · ${t.doctor}`, kv([["Now serving", t.now_serving ?? "not started"],
          ["Patients ahead", t.patients_ahead], ["Estimated turn", t.estimated_turn], ["Arrive by", t.please_arrive_by],
          ["Doctor late", t.doctor_running_late_minutes ? `${t.doctor_running_late_minutes} min` : null]]));
        else if (r.doctors) card("", "🏥", `OPD · ${r.weekday}`, r.doctors.map(d => d.sessions.map(s =>
          `<div class="doc-line"><b>${esc(d.doctor)}</b><span>${esc(s.label)} · ${s.cancelled ? "cancelled" : s.now_serving ? "now serving " + s.now_serving : s.ended ? "ended" : s.waiting + " waiting"}${s.delay_minutes ? " · " + s.delay_minutes + " min late" : ""}</span></div>`).join("")).join(""));
        break;
      }
      case "cancel_opd_token":
        if (r.cancelled) card("", "✕", `Token ${r.token_number} cancelled`, "");
        break;
      case "find_doctor":
        if (r.possible_emergency) card("alert", "!", `If this is happening now, call ${cfg.emergency_number || "108"}`, `<span>Signs like ${esc(r.possible_emergency.join(", "))} need emergency care.</span>`);
        if (r.department) card("", "🩺", `${esc(r.department)}`, (r.doctors || []).map(d =>
          `<div class="doc-line"><b>${esc(d.name)}</b><span>${esc(d.specialty)} · ${esc(d.consultation_fee)}</span></div>`).join("") +
          (r.follow_up ? `<span class="muted small">Follow-up: ${esc(r.follow_up)}</span>` : ""));
        break;
      case "search_clinic_knowledge":
        if ((r.citations || []).length) extra(`📄 Answered from ${r.citations.slice(0, 2).map(esc).join(" · ")}`, "source");
        break;
      case "request_callback":
        if (r.requested) card("ok", "☎", "The clinic will call you back", "<span>Your request is with the staff.</span>");
        break;
      case "transfer_to_human":
        if (r.emergency) card("alert", "!", `Emergency: call ${cfg.emergency_number || "108"} now`, "<span>Connecting you to the clinic staff as well.</span>");
        else note("Connecting you to the clinic staff…");
        break;
    }
  }

  // ------------------------------------------------------------ audio out (16 kHz PCM16)
  function playPcm(buf) {
    if (!voiceReplies) return;
    if (!playCtx) playCtx = new AudioContext({ sampleRate: 16000 });
    const i16 = new Int16Array(buf), f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    const ab = playCtx.createBuffer(1, f32.length, 16000);
    ab.copyToChannel(f32, 0);
    const src = playCtx.createBufferSource();
    src.buffer = ab; src.connect(playCtx.destination);
    nextPlayTime = Math.max(nextPlayTime, playCtx.currentTime + 0.03);
    src.start(nextPlayTime); nextPlayTime += ab.duration;
    active.push(src);
    src.onended = () => { active = active.filter(s => s !== src); if (!active.length && inCall) setState(mode === "voice" ? "listening" : "idle", mode === "voice" ? "Listening…" : "Ready"); };
    setState("speaking", "Speaking…");
  }
  function stopPlayback() { active.forEach(s => { try { s.stop(); } catch (_) {} }); active = []; if (playCtx) nextPlayTime = playCtx.currentTime; }

  // ------------------------------------------------------------ server events
  function onEvent(ev) {
    switch (ev.type) {
      case "ready": break;
      case "user":
        if (typedPending !== null) { if (ev.final) typedPending = null; break; }  // typed: already shown
        if (!userBubble) userBubble = row("user", "", "partial");
        setBubble(userBubble, ev.text, ev.final);
        if (ev.final) { userBubble = null; showTyping(); }
        break;
      case "assistant": {
        hideTyping();
        const text = ev.text.slice(consumed).trim();
        if (text && !botBubble) botBubble = row("assistant", "", "partial");
        if (botBubble) setBubble(botBubble, text, ev.final);
        if (ev.final && greetedResolve) { greetedResolve(); greetedResolve = null; }
        if (ev.final) { botBubble = null; consumed = 0; }
        if (ev.final) { if (!active.length) setState(inCall && mode === "voice" ? "listening" : "idle", inCall && mode === "voice" ? "Listening…" : "Ready"); }
        break;
      }
      case "tool":
        // close the "one moment" bubble so the answer appears below the card
        if (botBubble) { consumed += botBubble.textContent.length + 1; setBubble(botBubble, botBubble.textContent, true); botBubble = null; }
        onTool(ev); showTyping(); break;
      case "metrics": {
        const el = $("latency");
        el.textContent = `${(ev.latency_ms / 1000).toFixed(1)} s`;
        el.className = ev.latency_ms < 1500 ? "ok" : ev.latency_ms < 2500 ? "warn" : "bad";
        break;
      }
      case "interrupted": if (botBubble) { setBubble(botBubble, botBubble.textContent + " …", true); botBubble = null; }
        hideTyping(); if (inCall && mode === "voice") setState("listening", "Listening…"); break;
      case "listening": hideTyping(); if (inCall) setState(mode === "voice" ? "listening" : "idle", mode === "voice" ? "Listening…" : "Ready"); break;
      case "clear": stopPlayback(); break;
      case "hangup": note("The conversation has ended."); break;
      case "transfer": break;
      case "error": hideTyping(); note(ev.message, true); break;
    }
  }

  // ------------------------------------------------------------ connection
  function connect(wantMode) {
    if (ws && ws.readyState <= 1) return connecting || Promise.resolve();
    mode = wantMode;
    voiceReplies = wantMode === "voice" ? true : voiceReplies;
    $("voiceToggle").setAttribute("aria-pressed", String(voiceReplies));
    connecting = new Promise((resolve, reject) => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/browser`);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => ws.send(JSON.stringify({ type: "start", clinic: CLINIC || undefined, language: lang(),
        phone: $("phone").value.trim(), email: $("email").value.trim(), code: $("code").value }));
      ws.onmessage = (e) => {
        if (typeof e.data !== "string") return playPcm(e.data);
        const ev = JSON.parse(e.data);
        if (ev.type === "ready") { inCall = true; setState(mode === "voice" ? "listening" : "idle", mode === "voice" ? "Listening…" : "Ready"); updateButtons(); resolve(); }
        if (ev.type === "error" && !inCall) reject(new Error(ev.message));
        onEvent(ev);
      };
      ws.onclose = () => { finish(); reject(new Error("closed")); };
      ws.onerror = () => note("Connection problem. Please try again.", true);
    }).finally(() => { connecting = null; });
    return connecting;
  }

  async function startVoice() {
    $("callBtn").disabled = true;
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
      micCtx = new AudioContext();
      await micCtx.audioWorklet.addModule("/static/mic-worklet.js");
    } catch (e) {
      note("Microphone not available, so you can type instead.", true);
      cleanupMic(); $("callBtn").disabled = false; return;
    }
    playCtx = playCtx || new AudioContext({ sampleRate: 16000 });
    if (inCall) {  // already chatting: keep the same conversation and switch it to voice
      mode = "voice"; voiceReplies = true; $("voiceToggle").setAttribute("aria-pressed", "true");
      setState("listening", "Listening…");
    } else {
      try { await connect("voice"); } catch (e) { cleanupMic(); $("callBtn").disabled = false; return; }
    }
    const src = micCtx.createMediaStreamSource(micStream);
    micNode = new AudioWorkletNode(micCtx, "mic-processor");
    micNode.port.onmessage = (e) => {
      if (ws && ws.readyState === 1) ws.send(e.data.pcm);
      $("orb").style.transform = `scale(${1 + Math.min(0.22, e.data.level * 4)})`;
    };
    src.connect(micNode);
    $("callBtn").disabled = false;
    updateButtons();
  }

  function cleanupMic() {
    if (micNode) micNode.disconnect();
    if (micStream) micStream.getTracks().forEach(t => t.stop());
    if (micCtx) micCtx.close().catch(() => {});
    micNode = micStream = micCtx = null;
    $("orb").style.transform = "";
  }
  function endConversation(sendHangup = true) {
    if (sendHangup && ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "hangup" }));
    if (ws) { try { ws.close(); } catch (_) {} }
    finish();
  }
  function finish() {
    const was = inCall;
    inCall = false; ws = null;
    cleanupMic(); hideTyping(); stopPlayback();
    botBubble = userBubble = null; typedPending = null;
    setState("idle", was ? "Conversation ended" : "Ready to help");
    updateButtons();
  }
  function updateButtons() {
    const live = inCall && mode === "voice";
    const b = $("callBtn");
    b.classList.toggle("end", live);
    b.querySelector("span").textContent = live ? "End call" : "Start voice call";
    $("micBtn").classList.toggle("live", live);
    $("micBtn").title = live ? "End call" : "Voice call";
  }

  // ------------------------------------------------------------ sending text
  let greeted = null;  // resolves when the receptionist's greeting has arrived
  async function sendText(text) {
    text = (text || "").trim();
    if (!text) return;
    input.value = ""; autosize(); sendBtn.disabled = true;
    stopPlayback();
    try {
      if (!inCall) {
        greeted = new Promise(r => { greetedResolve = r; setTimeout(r, 2500); });
        showTyping();
        await connect("chat");
        await greeted;  // show the greeting first, then the caller's message
      }
      typedPending = text;
      setBubble(row("user", ""), text, true);
      showTyping();
      ws.send(JSON.stringify({ type: "text", text }));
    } catch (e) {
      hideTyping(); typedPending = null;
      note(e.message && e.message !== "closed" ? e.message : "Couldn't connect. Please try again.", true);
    }
  }
  function autosize() { input.style.height = "auto"; input.style.height = Math.min(140, input.scrollHeight) + "px"; sendBtn.disabled = !input.value.trim(); }

  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendText(input.value); } });
  $("composer").onsubmit = (e) => { e.preventDefault(); sendText(input.value); };
  const toggleVoice = () => (inCall && mode === "voice" ? endConversation(true) : startVoice());
  $("callBtn").onclick = toggleVoice;
  $("micBtn").onclick = toggleVoice;
  $("voiceToggle").onclick = () => {
    voiceReplies = !voiceReplies;
    $("voiceToggle").setAttribute("aria-pressed", String(voiceReplies));
    if (!voiceReplies) stopPlayback();
  };
  $("stepsToggle").onclick = () => {
    const on = msgs.classList.toggle("show-steps");
    $("stepsToggle").setAttribute("aria-pressed", String(on));
  };
  $("newChat").onclick = () => {
    endConversation(true);
    msgs.innerHTML = ""; lastRole = null; $("welcome").hidden = false;
    setState("idle", "Ready to help");
  };
  // chat mode starts with voice replies off (typing is usually silent); the speaker button turns them on
  $("voiceToggle").setAttribute("aria-pressed", "false"); voiceReplies = false;
})();
