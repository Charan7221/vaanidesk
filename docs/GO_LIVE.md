# Going live: Google Calendar → Online (Render) → Phone number (Twilio)

Do the steps in order. Each step works on its own, and you can stop after any of them.

---

## Step 0: Client calendar with zero setup (recommended for clients)

The owner doesn't need to connect anything:

- **Dashboard → Calendar** (`/calendar`) shows the week for each doctor. Staff click a free slot to **block time** (meeting, lunch, leave; one doctor or the whole clinic) or to **book a walk-in**. Click an appointment to cancel it. The AI never offers blocked or booked slots.
- **"See appointments on your phone"** at the bottom of that page gives a private calendar link. Subscribing to it once in Google Calendar, Apple Calendar or Outlook shows every booking on the owner's phone. This needs VaaniDesk to be online (Step 2), because phones can't reach `localhost`. Google refreshes subscribed calendars every few hours, so treat it as a viewing copy; the dashboard is always live.

Step 1 below is optional, for clinics that want two-way sync with an existing Google Calendar.

## Step 1: Google Calendar two-way sync (optional, about 15 minutes, free)

**What you get:** every booking the AI makes appears in a Google Calendar, which you can see on your phone. Anything already in that calendar (for example "Doctor's meeting 6–7 PM") blocks those slots, so the AI won't offer them. Cancelling from the dashboard also removes the event.

### 1a. Create a calendar for the clinic
1. Open **calendar.google.com** → left side, **Other calendars → +** → **Create new calendar**.
2. Name it, for example *Sunrise Clinic – Appointments*. Time zone: **(GMT+05:30) India Standard Time** → **Create calendar**.
3. Open the new calendar's **Settings** and scroll to **Integrate calendar** → copy the **Calendar ID** (it looks like `abc123...@group.calendar.google.com`).

### 1b. Create a Google Cloud service account (the "robot" that writes to the calendar)
1. Open **console.cloud.google.com** → top bar project picker → **New project** → name it `vaanidesk` → **Create** and select it.
2. Search for **Google Calendar API** → **Enable**.
3. Go to **IAM & Admin → Service Accounts → Create service account** → name it `vaanidesk-calendar` → **Create and continue** → **Done**. No roles are needed.
4. Click the new service account → **Keys** tab → **Add key → Create new key → JSON** → **Create**. A `.json` file downloads.
5. Copy the service account's **email** (`vaanidesk-calendar@vaanidesk-xxxx.iam.gserviceaccount.com`).

> If key creation is blocked with a "Service account key creation is disabled" policy message, your account belongs to an organisation that forbids keys. Use a personal Gmail account instead.

### 1c. Give the robot access to the calendar
1. Back in Google Calendar → your clinic calendar → **Settings → Share with specific people → Add people**.
2. Paste the service account email and choose **Make changes to events** → **Send**.

### 1d. Connect VaaniDesk
1. In the `vaanidesk` folder create a folder named `secrets` and move the downloaded JSON file into it as `secrets/service-account.json`. The `secrets/` folder is git-ignored, so it is never uploaded.
2. Add to `.env`:
   ```
   GOOGLE_SERVICE_ACCOUNT_FILE=secrets/service-account.json
   GOOGLE_CALENDAR_ID=abc123...@group.calendar.google.com
   ```
3. Test: `python -m scripts.check_setup --google` should print `[PASS] Google Calendar  connected to calendar '...'`.
4. Restart uvicorn and book an appointment by voice. The event should appear in Google Calendar within a few seconds.
5. Try the blocking: add an event "Meeting" tomorrow 5–6 PM in that calendar, then ask the AI for tomorrow evening. It should start offering slots at 6:00.

**Security:** the JSON key can edit only calendars you shared with it. Never commit it to GitHub or paste it in chats. If it leaks, delete the key in Cloud Console → Service account → Keys.

---

## Step 2: Put it online with Render (about 30 minutes)

**What you get:** a public HTTPS URL (`https://vaanidesk.onrender.com`) for the demo and dashboard, with bookings stored in PostgreSQL. This is also required for Step 3, because Twilio needs a public URL.

### 2a. Push the code to GitHub
```bash
git init
git add .
git status            # make sure .env and secrets/ are NOT listed
git commit -m "VaaniDesk: real-time multilingual voice AI receptionist"
```
Create an empty repository on github.com named `vaanidesk`, then:
```bash
git remote add origin https://github.com/<your-username>/vaanidesk.git
git branch -M main
git push -u origin main
```

### 2b. Deploy the Blueprint
1. Sign up at **render.com** with your GitHub account.
2. **New → Blueprint** → select the `vaanidesk` repo. Render reads `render.yaml` and creates the web service and the Postgres database.
3. Fill the secret values it asks for:
   - `OPENAI_API_KEY`: your Gemini key
   - `SARVAM_API_KEY`
   - `ADMIN_PASSWORD`: a strong password
   - `DEMO_ACCESS_CODE`: a short code you give to recruiters (protects your API credits)
   - `GOOGLE_CALENDAR_ID`: same as in `.env`
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: open your JSON key file, copy **all** its text, and paste it
   - Twilio values: leave empty for now
4. **Apply** and wait for the build to finish (5–10 min). Then open `https://<your-service>.onrender.com/health`.

**Notes:** check Render's pricing page before relying on the free tier. Free web services sleep when idle (the first visit takes about a minute to wake), and free databases have time limits. For an always-on demo, use a paid instance type.

---

## Step 3: A real phone number with Twilio (about 20 minutes, paid)

**What you get:** people dial a phone number and talk to the AI. Bookings, transcripts and SMS confirmations work exactly as in the browser.

1. Sign up at **twilio.com** and verify your own mobile number.
2. **Upgrade** the account (add a payment method). Trial accounts play a trial message, can only call verified numbers, and may not support live audio streaming.
3. **Phone Numbers → Buy a number** with **Voice** capability. Indian (+91) numbers need regulatory documents. A US number is quickest for a demo, but callers from India then pay international rates.
4. Copy **Account SID** and **Auth Token** from the Console home page. Add them and the number to Render's environment (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` in E.164 format, e.g. `+14155550123`) → Save, and the service redeploys.
5. **Phone Numbers → Active numbers → your number → Voice configuration → "A call comes in" → Webhook**, `https://<your-service>.onrender.com/twilio/voice`, **HTTP POST** → Save.
6. Call the number.

**Testing locally before deploying:** run `ngrok http 8000`, set `PUBLIC_BASE_URL=https://<id>.ngrok-free.app` in `.env`, restart uvicorn, and use `https://<id>.ngrok-free.app/twilio/voice` as the webhook.

**SMS confirmations** need the caller's number in international format (`+91...`). Phone callers' numbers come from Twilio automatically.
