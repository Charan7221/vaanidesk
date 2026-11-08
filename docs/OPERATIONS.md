# Running VaaniDesk for a real clinic

This is the checklist for taking a clinic from demo to a supervised pilot and then to production.

## 0. Adding a new clinic (multi-clinic server)

1. Log in as the platform admin (`ADMIN_USER` / `ADMIN_PASSWORD`) and open **Clinics** (`/admin`).
2. **Add a clinic:** an id (used in links), its name, a template (small clinic or multispeciality hospital), the owner's email and a temporary password, and the clinic's phone number(s).
3. The owner logs in, changes the password (Setup → Staff → Reset password), and fills in **Setup**: doctors, hours, departments and problems they treat, fees, FAQs, pilot mode. They add receptionists under **Staff**.
4. The owner uploads the clinic's documents (fees, insurance, preparation instructions) on **Knowledge**.
5. Test with fake patients on the clinic's demo link `/demo?clinic=<id>`, then connect the phone number (GO_LIVE Step 4).

Each clinic only ever sees its own calls, patients, bookings, tokens, documents and staff. Every change is in **Setup → Activity log**.

## 1. Before the pilot

- [ ] **Postgres, not SQLite.** Use a paid Render Postgres (or any managed Postgres) with automatic backups. The free Render database expires.
- [ ] **Paid web instance** (always on). Free instances sleep, and the first caller after a sleep waits about a minute.
- [ ] **Clinic data:** doctors, departments, hours, fees and FAQs in `config/business.yaml`. The clinic's own documents go in `knowledge/`. Have the clinic sign off on every fact.
- [ ] **`pilot_mode: true`.** Every AI booking or token waits on the dashboard until staff press **Confirm**.
- [ ] **Staff alerts:** set `staff_alert_phone` to the reception mobile. Every new follow-up sends an SMS or WhatsApp (needs Twilio).
- [ ] **Strong passwords:** `ADMIN_PASSWORD` (12+ characters) and `DEMO_ACCESS_CODE`. Never share the admin password with patients.
- [ ] **Consent:** keep `consent_notice: true`. Agree with the clinic how long transcripts are kept (`transcript_retention_days`) and record it in your agreement with them.
- [ ] **Test with fake patients first:** run `docs/VERIFICATION.md` with made-up names and numbers before real patients call.

## 2. Every day during the pilot

Open the dashboard. The **Needs attention** panel lists:

| Item | What it means | What staff do |
|---|---|---|
| Pending booking / token | The AI booked it; pilot mode is on | Check it, then **Confirm** (the patient gets a confirmation SMS) or **Reject** (it's cancelled and the patient is asked to call) |
| AI failed mid-call | Gemini or Sarvam failed; the caller heard an apology | Call the patient back |
| Call ended before booking | The caller hung up during a booking | Call back if needed |
| Asked for a call back | The AI couldn't help, or it was after hours | Call back |

Nothing disappears until someone presses **Done**, so no request is lost.

## 2b. Showing the clinic what it gets

Every week (or before a renewal), open **Results**, choose the period and press **Print / save as PDF**. Send it to the clinic owner. The headline ("answered N calls, about M would likely have been missed") and the consultation fees from AI bookings are what justify the subscription.

## 2c. Patient messages

- Look at **Dashboard → Messages to patients** each morning. "Failed" (hover for the reason) usually means a wrong number, a WhatsApp template problem, or SMS blocked by DLT. "Not set up" means no provider is configured for that channel (see GO_LIVE Step 5).
- If a patient says they didn't get a confirmation, use **Message** on their appointment row to resend the details.

## 3. Backups and the restore drill

```bash
python -m scripts.backup export backups/vaanidesk-$(date +%F).json.gz   # daily, then copy the file off the server
python -m scripts.backup verify backups/vaanidesk-YYYY-MM-DD.json.gz    # restore drill into a temporary database
python -m scripts.backup restore backups/vaanidesk-YYYY-MM-DD.json.gz   # disaster recovery, into an EMPTY database
```

On Render, add a **Cron Job** that runs the export daily and uploads it to storage you control. Also rely on the managed Postgres point-in-time backups. **Do a restore drill before the pilot and once a month.** A backup that has never been restored doesn't count.

## 4. Monitoring

- **Uptime:** point an uptime monitor (UptimeRobot, Better Stack and similar) at `https://<your-app>/health` every minute. Alert if it isn't `200` or if `missing_keys` isn't empty.
- **Errors:** Render logs. Search for `response failed` (AI outage), `Tool ... failed`, `Summary failed`.
- **Follow-ups:** a rising count of *AI failed mid-call* means a provider problem. Check the Gemini or Sarvam status and quota.

## 5. What happens when something breaks

| Failure | What the caller experiences | What staff see |
|---|---|---|
| LLM or voice provider down or out of quota | "Sorry, I'm having a technical problem." On the phone they're transferred to `handoff_number`; in the browser they're told staff will call back | *AI failed mid-call* follow-up, plus an SMS alert |
| Two callers try to book the same slot at the same moment | The second hears that the slot was just taken, with alternatives | Nothing to do: the database allows one booking per doctor per slot |
| Caller hangs up mid-booking | – | *Call ended before booking* follow-up |
| Server redeploys | Live calls drop (keep deploys outside clinic hours) | Data is in Postgres and survives |

## 6. Leaving pilot mode

Switch `pilot_mode` off only when, over at least two weeks of real calls:

- staff confirmed nearly every AI booking without changes,
- no booking was wrong (doctor, time or patient),
- every follow-up was handled the same day,
- the clinic agrees in writing.
