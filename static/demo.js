// Browser voice client for VaaniDesk.
(() => {
  const $ = (id) => document.getElementById(id);
  const transcriptEl = $("transcript");
  const orb = $("orb");
  const statusEl = $("status");
  const callBtn = $("callBtn");
  const textInput = $("textInput");
  const sendBtn = $("sendBtn");

  let ws = null;
  let micCtx = null, micStream = null, micNode = null;
  let playCtx = null, nextPlayTime = 0;
  let activeSources = [];
  let partialUser = null, partialAssistant = null;
  let inCall = false;

  fetch("/api/public-config").then(r => r.json()).then(cfg => {
    document.title = `${cfg.business} · VaaniDesk`;
    $("intro").textContent = `Talk to ${cfg.assistant}, the AI receptionist for ${cfg.business}, in English, हिन्दी or తెలుగు. Try: “I want to book an appointment tomorrow evening.”`;
    if (cfg.access_code_required) $("codeField").hidden = false;
    if (cfg.phone_number) $("phoneLine").textContent = `Or call ${cfg.phone_number} from your phone.`;
  }).catch(() => {});

  function setStatus(text, speaking) {
    statusEl.textContent = text;
    orb.classList.toggle("speaking", !!speaking);
    orb.classList.toggle("idle", !inCall);
  }

  function addMsg(role, text, cls = "") {
    const div = document.createElement("div");
    div.className = `msg ${role} ${cls}`.trim();
    div.textContent = text;
    transcriptEl.appendChild(div);
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
    return div;
  }

  // ---------------- playback (16 kHz PCM16 from server)
  function playPcm(buf) {
    if (!playCtx) return;
    const int16 = new Int16Array(buf);
    const f32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 32768;
    const audioBuf = playCtx.createBuffer(1, f32.length, 16000);
    audioBuf.copyToChannel(f32, 0);
    const src = playCtx.createBufferSource();
    src.buffer = audioBuf;
    src.connect(playCtx.destination);
    nextPlayTime = Math.max(nextPlayTime, playCtx.currentTime + 0.03);
    src.start(nextPlayTime);
    nextPlayTime += audioBuf.duration;
    activeSources.push(src);
    src.onended = () => {
      activeSources = activeSources.filter(s => s !== src);
      if (!activeSources.length && inCall) setStatus("Listening…", false);
    };
    setStatus("Speaking…", true);
  }

  function stopPlayback() {
    activeSources.forEach(s => { try { s.stop(); } catch (_) {} });
    activeSources = [];
    if (playCtx) nextPlayTime = playCtx.currentTime;
  }

  // ---------------- server events
  function onEvent(ev) {
    switch (ev.type) {
      case "ready": setStatus("Connected", false); break;
      case "user":
        if (!partialUser) partialUser = addMsg("user", ev.text, "partial");
        partialUser.textContent = ev.text;
        if (ev.final) { partialUser.classList.remove("partial"); partialUser = null; }
        break;
      case "assistant":
        if (!partialAssistant) partialAssistant = addMsg("assistant", ev.text, "partial");
        partialAssistant.textContent = ev.text;
        if (ev.final) { partialAssistant.classList.remove("partial"); partialAssistant = null; }
        break;
      case "tool":
        if (ev.name === "search_clinic_knowledge") {
          const cites = (ev.result && ev.result.citations) || [];
          addMsg("event", `🔎 Looked up “${(ev.args || {}).query}” → ${cites.length ? cites.map(c => "📄 " + c).join("  ") : "nothing found"}`);
        } else {
          addMsg("event", `⚙ ${ev.name}(${JSON.stringify(ev.args)}) → ${JSON.stringify(ev.result)}`);
        }
        break;
      case "metrics": {
        const el = $("latency");
        el.textContent = `${ev.latency_ms} ms`;
        el.className = "badge " + (ev.latency_ms < 1200 ? "ok" : ev.latency_ms < 2000 ? "warn" : "bad");
        break;
      }
      case "interrupted":
        if (partialAssistant) { partialAssistant.classList.remove("partial"); partialAssistant.textContent += " …"; partialAssistant = null; }
        addMsg("event", "caller interrupted — agent stopped speaking");
        break;
      case "clear": stopPlayback(); break;
      case "transfer": addMsg("event", `↪ transferring to staff (${ev.number})`); break;
      case "hangup": addMsg("event", "call ended by assistant"); break;
      case "error": addMsg("event", `error: ${ev.message}`); break;
    }
  }

  // ---------------- call control
  async function startCall() {
    callBtn.disabled = true;
    setStatus("Connecting…", false);
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
    } catch (e) {
      addMsg("event", "Microphone permission denied — you can still type messages.");
    }
    playCtx = new AudioContext({ sampleRate: 16000 });
    nextPlayTime = 0;
    if (micStream) {
      try {
        micCtx = new AudioContext();
        await micCtx.audioWorklet.addModule("/static/mic-worklet.js");
      } catch (e) {
        addMsg("event", `Microphone setup failed (${e.name}) — you can still type messages.`);
        micCtx = null;
      }
    }

    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/browser`);
    ws.binaryType = "arraybuffer";
    ws.onopen = async () => {
      inCall = true;
      ws.send(JSON.stringify({ type: "start", language: $("lang").value, phone: $("phone").value.trim(), code: $("code").value }));
      if (micStream && micCtx) {
        const src = micCtx.createMediaStreamSource(micStream);
        micNode = new AudioWorkletNode(micCtx, "mic-processor");
        micNode.port.onmessage = (e) => {
          if (ws && ws.readyState === 1) ws.send(e.data.pcm);
          orb.style.transform = `scale(${1 + Math.min(0.25, e.data.level * 4)})`;
        };
        src.connect(micNode);
      }
      callBtn.textContent = "End call";
      callBtn.className = "danger";
      callBtn.disabled = false;
      textInput.disabled = sendBtn.disabled = false;
      setStatus("Listening…", false);
    };
    ws.onmessage = (e) => {
      if (typeof e.data === "string") onEvent(JSON.parse(e.data));
      else playPcm(e.data);
    };
    ws.onclose = () => endCall(false);
    ws.onerror = () => addMsg("event", "connection error");
  }

  function endCall(sendHangup = true) {
    if (!inCall && !ws) return;
    inCall = false;
    if (sendHangup && ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "hangup" }));
    if (ws) { try { ws.close(); } catch (_) {} ws = null; }
    if (micNode) micNode.disconnect();
    if (micStream) micStream.getTracks().forEach(t => t.stop());
    if (micCtx) micCtx.close();
    setTimeout(() => { if (playCtx && !inCall) { playCtx.close(); playCtx = null; } }, 1500);
    micNode = micStream = micCtx = null;
    callBtn.textContent = "Start call";
    callBtn.className = "primary";
    callBtn.disabled = false;
    textInput.disabled = sendBtn.disabled = true;
    orb.style.transform = "";
    setStatus("Call ended", false);
  }

  callBtn.onclick = () => (inCall ? endCall(true) : startCall());
  $("composer").onsubmit = (e) => {
    e.preventDefault();
    const text = textInput.value.trim();
    if (!text || !ws || ws.readyState !== 1) return;
    stopPlayback();
    ws.send(JSON.stringify({ type: "text", text }));
    textInput.value = "";
  };
})();
