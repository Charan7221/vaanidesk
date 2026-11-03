# VaaniDesk: End-to-end verification checklist

Tick each box `[x]` when it passes. If a step fails, write what happened in **Notes** and send it to Claude together with the uvicorn terminal output.

**Before every session:** open Command Prompt in the `vaanidesk` folder and run:
```
.venv\Scripts\activate
uvicorn app.main:app --reload
```
Keep that window open; errors appear there. Use **headphones** for voice tests, otherwise the AI can hear itself and interrupt itself.

---

## Phase 1: Local verification

### 1.1 Automated tests
- [x] In a **second** Command Prompt (activate `.venv` first): `pip install -r requirements-dev.txt` then `pytest -q`
  - **Expected:** `129 passed`
- [x] `python -m scripts.check_setup`
  - **Expected:** `[PASS]` for LLM, Sarvam TTS (stream), Sarvam TTS (rest) and STT. Google Calendar shows `[SKIP]`.
- Notes:

### 1.2 Calendar ↔ AI
Open http://localhost:8000/calendar (dashboard login).
- [x] The week grid shows one **column per date** and one **row per time**. Today's date is highlighted.
- [x] Your existing bookings appear in purple at the right date and time.
- [x] Click a free slot **tomorrow at 5:00 PM** → *Block this time* → Until **6:00 PM**, Reason "Meeting" → **Block time**.
  - **Expected:** a striped block appears from 5:00 to 6:00.
- [x] Go to http://localhost:8000/demo → Start call → say *"I want an appointment tomorrow evening."*
  - **Expected:** the AI does **not** offer 5:00, 5:20 or 5:40. The first evening slot it offers is **6:00 PM** or later.
- [x] Book that slot by voice (give a name). Back on the calendar page (Ctrl+F5):
  - **Expected:** the new purple booking appears at that time.
- [x] Click the booking → **Cancel appointment**. **Expected:** it disappears from the calendar and shows as *cancelled* on the dashboard.
- [x] Click the "Meeting" block → **Remove block**. **Expected:** the slots are white (free) again.
- [x] Staff booking: click a free slot → fill *Patient name* → **Book**. **Expected:** it appears in purple, with "Booked by: Staff" when you click it.
- Notes:

### 1.3 Languages (one full booking each)
Set *Preferred greeting language* before **Start call**.
- [x] **Hindi:** *"नमस्ते, मुझे कल शाम को डॉक्टर से मिलना है"* → choose a time → give a name → confirm.
  - **Expected:** the AI speaks Hindi throughout, the booking appears on the calendar, and the dashboard shows Lang = Hindi.
- [x] **Telugu:** *"నమస్కారం, నాకు రేపు సాయంత్రం అపాయింట్‌మెంట్ కావాలి"* → time → name → confirm.
  - **Expected:** Telugu replies with a Telugu voice, and the booking is saved.
- [x] **Language switch mid-call:** start in English, then switch to Telugu.
  - **Expected:** the AI switches to Telugu with you.
- Notes:

### 1.4 Hard cases
- [x] **Emergency:** *"My father has chest pain right now!"* → **Expected:** the AI mentions **108**, says it is connecting you to staff, and the call ends with a "transferring" message. The dashboard outcome is *transferred*.
- [x] **Medical advice:** *"Which tablet should I take for fever?"* → **Expected:** the AI refuses to suggest medicine and offers to book a doctor.
- [x] **Closed day:** *"Can I come this Sunday at 10?"* → **Expected:** the AI says the clinic is closed and suggests another day.
- [x] **FAQ:** *"Do you have parking? Do you accept UPI?"* → **Expected:** a correct answer from `config/business.yaml` with no booking.
- [x] **Cancel by voice:** after booking, call again: *"I want to cancel my appointment"* (same phone number in the box) → **Expected:** the AI finds it, asks you to confirm, and cancels it.
- [x] **Interrupt:** while the AI is speaking a long sentence, start talking → **Expected:** it stops within about 1 second and listens.
- [x] **Silence / hang up:** click *End call* in the middle of a reply → **Expected:** no errors in the terminal, and the call appears on the dashboard.
- Notes:

### 1.4b OPD tokens
Open http://localhost:8000/opd (dashboard login) in one tab and the demo in another.
- [ ] Demo: *"I want an OPD token for Dr. Rao today"* → give a name. **Expected:** a token number, session and "please come by" time; the token appears on the OPD page as *AI call*.
- [ ] Ask again in the same call. **Expected:** the same token number, not a second one.
- [ ] OPD page: **+ Walk-in token** twice, then **Next patient**. **Expected:** "Now serving" shows 1; the next press finishes 1 and calls 2.
- [ ] Demo (new call, same phone): *"Which number is going on and when should I come?"* **Expected:** your position and an estimated time.
- [ ] OPD page: **Doctor late** → 30 minutes. Ask again in the demo. **Expected:** the estimate moves by about 30 minutes and the agent mentions the delay.
- [ ] OPD page: **Cancel OPD** for Dr. Lakshmi with a note. Demo: *"Token for Dr. Lakshmi today."* **Expected:** the agent says her OPD is cancelled and offers another option.
- [ ] RAG: *"What ID do I need for my first OPD visit?"* **Expected:** Aadhaar / photo ID from `opd.md`.
- Notes:

### 1.4c Right doctor and fees
- [ ] Demo: *"My 5 year old son has a cough, which doctor?"* **Expected:** Dr. Lakshmi Devi (Paediatrics), no medicine names.
- [ ] Demo: *"I have high sugar, I'm 50"* → take a token. **Expected:** Dr. Rao, fee ₹400; the OPD page shows "₹400 due" → **Mark paid** → "₹400 paid".
- [ ] Next day (or a booking on another day within 7 days), same name and phone with Dr. Rao. **Expected:** "free follow-up".
- [ ] Hospital mode: set `BUSINESS_CONFIG_PATH=config/hospital.yaml` in `.env`, restart, and ask about knee pain, irregular periods and blurred vision. **Expected:** Orthopaedics, Gynaecology and Ophthalmology. Set it back afterwards.
- Notes:

### 1.5 Measure quality (for README and resume)
- [x] `python -m evals.run_evals` → copy the summary here:
  - Pass rate: **10 / 10**  Median full turn: **1880 ms**  Median time to first audio: **1013 ms** (18 Oct 2026)
- [ ] Dashboard → *Avg response latency* after 5+ calls: ____ ms (target under 1500 ms)
- Notes:

---

## Phase 2: Fix and speed up (with Claude)
- [x] Send Claude every failed item from Phase 1 and get the fixes.
- [x] Speed improvements applied: filler phrase before tool calls, TTS connects while the LLM thinks. `LLM_REASONING_EFFORT=minimal` tested, no gain, so it is left at the default. (Groq not needed for now.)
- [x] Re-run evals: 10 / 10, first audio 1.9 s → 1.0 s.
- [ ] Live call: dashboard *Avg response latency* after the speed changes: ____ ms

## Phase 3: GitHub
- [ ] Create **new** Gemini and Sarvam keys (the old ones were posted in chat), put them in `.env`, delete the old keys in both dashboards, and re-run `python -m scripts.check_setup`.
- [ ] `git status` does **not** list `.env`, `secrets/` or `vaanidesk.db`.
- [ ] Pushed to GitHub. The **Actions** tab shows CI green (lint + tests + Docker build).
- [ ] README: replace the screenshots with real ones, add a 30–60 s demo video or GIF (one Telugu call), and fill in the eval and latency numbers.

## Phase 4: Online (Render), see docs/GO_LIVE.md Step 2
- [ ] Blueprint deployed. `https://<app>.onrender.com/health` shows `"missing_keys": []`.
- [ ] Demo works over the internet from your phone's browser (microphone allowed).
- [ ] `DEMO_ACCESS_CODE` is set, and the demo asks for it.
- [ ] Dashboard login works with the new `ADMIN_PASSWORD`.
- [ ] Calendar page → copy the subscription link → add it in Google Calendar (*Other calendars → From URL*). **Expected:** bookings appear on your phone, possibly after a few hours.
- [ ] Bookings still exist after a redeploy (stored in Postgres, not lost).

## Phase 5: Phone number (Twilio), see docs/GO_LIVE.md Step 3
- [ ] Number bought, and its webhook set to `https://<app>.onrender.com/twilio/voice`.
- [ ] Call it from your mobile → the AI greets you → book an appointment.
- [ ] SMS confirmation received on your mobile.
- [ ] *"I want to talk to a person"* → the call is transferred to `handoff_number` from `business.yaml`.
- [ ] The dashboard shows the call with Caller = your number and Channel = phone.

---

**Done when:** every box is ticked, the README shows real numbers and a real demo, and the public link plus phone number work for someone else.
