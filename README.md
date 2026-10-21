# MediSlot – AI Voice Receptionist + Notifications (development prototype)

Module owner: AI Voice Receptionist + Notifications. It **integrates with** Akhil's existing n8n HMS API and does **not**
rebuild doctors / schedules / availability.

> **Status: development prototype. Not production-certified for healthcare use.** Use fictional data only.
> What was actually verified is listed honestly in section 9.

## 1. Folder structure
```
medislot/
  app/
    main.py  config.py  container.py  db.py  security.py  logging_config.py  utils.py
    api/        chat.py  voice_webhooks.py  callbacks.py  summaries.py
    services/   hms_api.py  mock_hms.py  appointment_service.py  openai_service.py  receptionist_service.py
                faq_service.py  handoff_service.py  notification_service.py  reminder_service.py
                voice_service.py  summary_service.py
    tools/      appointment_tools.py            # OpenAI tool schemas + trusted executor + confirmation gate
    models/     schemas.py  db_models.py
    repositories/ call_ callback_ reminder_ booking_ notification_repository.py
    providers/  telephony/{base,mock,twilio}.py  email/base.py  sms/base.py  factory.py
    data/hospital_faqs.json                     # SAMPLE answers - team must verify
  tests/        conftest.py + 6 test modules (78 tests)
  postman/MediSlot.postman_collection.json
  .env.example  .gitignore  requirements.txt  pytest.ini  README.md
```

## 2. Windows + VS Code setup (PowerShell)
```powershell
# 1. Install Python 3.12 from python.org (tick "Add python.exe to PATH") and Git. Open the folder in VS Code.
cd medislot
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -r requirements.txt
copy .env.example .env                 # then edit .env
```
In VS Code: `Ctrl+Shift+P` -> *Python: Select Interpreter* -> choose `.venv`.

Generate an internal key and paste it into `.env` as `INTERNAL_API_KEY=`:
```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
The database tables are created automatically at startup (SQLite file `medislot.db`). Nothing else to initialise.

## 3. Start the server
```powershell
uvicorn app.main:app --reload --port 8000
```
Open http://localhost:8000/docs (Swagger) and http://localhost:8000/health. `/health` shows which parts are **mock** vs **live**.

**With an empty `.env` (no keys) the app still runs in MOCK mode:** fictional local HMS, scripted mock "LLM" (FAQ,
doctor list, availability only), mock telephony (JSON instead of a phone), mock email/SMS (logged, nothing sent).
Real booking conversations need `OPENAI_API_KEY`.

## 4. Run the tests
```powershell
pytest -q
```
All external services are mocked (respx / mock providers). No real appointments or messages are created.

## 5. Configure the HMS integration
1. In `.env` set `HMS_API_KEY=<key from Akhil>`; keep `HMS_BASE_URL=https://akhil12.app.n8n.cloud/webhook`.
2. The key is sent only as the `x-api-key` header from the server; it is redacted from logs and never sent to the model.
   **Two ways to reach Akhil's system:** (a) the n8n webhook (`HMS_BASE_URL=https://akhil12.app.n8n.cloud/webhook`,
   `HMS_PATH_PREFIX=/hms`) or (b) his TypeScript/Express scheduling service, whose routes are `/api/doctors`, `/api/availability`,
   `/api/appointments` (`HMS_BASE_URL=http://localhost:4000/api`, `HMS_PATH_PREFIX=` empty). Both return `{success, data}` /
   `{success:false, error:{code,message,details}}`; this client reads `SLOT_TAKEN` (+ `details.alternatives`) and `DOCTOR_ON_LEAVE`.
3. **Validate the contract against the live service** (I could not reach it from my environment). With the server running:
   - `GET /hms/doctors?active=true`, `GET /hms/availability?specialty=Pediatrics&date=<future date>`,
     `GET /hms/availability?doctor_id=DOC-001&date=<future date>` – confirm the response *shapes*. `parse_doctors` /
     `parse_availability` in `app/services/hms_api.py` accept several common shapes (`data.doctors`, `slots` as strings or
     objects); adjust them if the real payload differs.
   - Book one **test** appointment, then cancel it; confirm: status codes for conflict (code assumes **409**, also treats
     400/422 with "taken/booked/unavailable" wording as conflict), whether `Idempotency-Key` is honoured, what a leave
     response (422) body contains, and the cancel response body.
4. Booking safety built in: operation is recorded `in_flight` before POST; timeout/5xx on POST = **uncertain**, never
   retried, staff alerted; 2xx without an appointment id is treated as uncertain, not confirmed.

## 6. Voice calling
**Provider comparison (short):**
| Option | Pros | Cons |
|---|---|---|
| **Twilio (default)** | Inbound numbers, simple TwiML webhooks, built-in speech recognition (`<Gather>`) and TTS (`<Say>`), documented HMAC signatures, huge docs | India inbound numbers need regulatory documents/bundles; per-minute cost |
| Exotel / Knowlarity | Indian numbers, local compliance | Smaller English docs; different webhook formats |
| Vonage / Plivo | Similar webhook model | Fewer tutorials; separate STT/TTS wiring |
| OpenAI Realtime + SIP trunk | Lowest latency, natural interruptions | More moving parts; not beginner-friendly |

**Design:** modular pipeline per turn: Twilio STT (`<Gather input="speech">`) -> `/api/calls/{id}/events` -> receptionist +
tools -> Twilio TTS (`<Say>`). Provider specifics are isolated in `app/providers/telephony/`; to switch provider implement
`TelephonyProvider` (5 methods) and register it in `providers/factory.py`.

**Activate live telephony (Twilio):**
1. Create a Twilio account, buy/enable a voice-capable number (check India regulatory requirements for your country).
2. Expose your laptop: `ngrok http 8000` -> copy the `https://xxxx.ngrok-free.app` URL.
3. `.env`: `TELEPHONY_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `PUBLIC_BASE_URL=https://xxxx.ngrok-free.app`
   (must match exactly what Twilio calls – it is part of the signature), `OPENAI_API_KEY`, `HMS_API_KEY`. Restart.
4. Twilio Console -> Phone Numbers -> your number -> **A call comes in**: Webhook, `POST`,
   `https://xxxx.ngrok-free.app/api/calls/inbound`. **Call status changes**: `POST`, `https://xxxx.ngrok-free.app/api/calls/status`.
5. Call the number. Requests with a missing/invalid `X-Twilio-Signature` get HTTP 403 and create nothing.
6. Optional `HANDOFF_DIAL_NUMBER` makes the call transfer to a human on handoff.

**Mock voice mode (no provider):** with `TELEPHONY_PROVIDER=mock` use the Postman requests "MOCK voice": POST
`/api/calls/inbound` returns what would be spoken plus `next_url`; POST `{"call_sid": "...", "speech": "..."}` to that URL; POST
`{"status": "completed"}` to finish and create the summary. This exercises everything except the phone network and Twilio's
STT/TTS. **A live call has not been tested – do not claim live voice works until you complete a real test call.**

## 7. Notifications
- Patient booking confirmation and reminders: **SMS** (to the number the caller gave). Staff alerts and call summaries: **email**
  (+ optional SMS to `STAFF_ALERT_PHONE`). Confirmation is sent **only after HMS returns a confirmed booking**.
- Email: `EMAIL_PROVIDER=smtp` + `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`
  (Gmail: use an app password). SMS: `SMS_PROVIDER=twilio` + Twilio SID/token + `TWILIO_PHONE_NUMBER`.
  Set `STAFF_ALERT_EMAIL`, `STAFF_ALERT_PHONE`, `CALL_SUMMARY_EMAIL`. Missing credentials => automatic mock provider (logged warning).
- Reminders: `REMINDER_OFFSET_HOURS=24` (IST). Stored in the `reminders` table, unique per (appointment, offset), polled every
  `REMINDER_POLL_SECONDS`; cancelled appointments cancel pending reminders; a booking made less than 24 h ahead is marked `skipped`.
- Delivery status is in the `notifications` table (`sent`/`failed`, masked recipient, unique dedupe key).
- Indian SMS delivery needs DLT registration with your provider; Twilio trial accounts only text verified numbers.

## 8. How the safety rules are enforced (in code, not only in the prompt)
- Booking: `prepare_booking` (live slot check) -> read-back -> `book_appointment` runs only if details match, a **new** caller
  message arrived, and that message is an explicit yes (`app/tools/appointment_tools.py`).
- Cancellation: verified only if appointment ID **and** name **and** phone match a booking MediSlot made. The HMS has no lookup
  endpoint, so anything else is **escalated to staff** (callback + alert). Cancellation is confirmed only after HMS success.
- Emergency keywords and "talk to a human" are handled **before** the model. Max tool calls per turn (`MAX_TOOL_CALLS_PER_TURN`).
- Transcripts are deleted when a call ends (`RETAIN_TRANSCRIPTS=false`); summaries contain no phone numbers; logs redact
  secrets and phone numbers.
- Internal endpoints need `X-Internal-Api-Key`; webhooks need a valid provider signature; rate limiting per client IP.

## 9. Feature checklist
| Feature | Status |
|---|---|
| HMS client (doctors, availability, book, cancel), idempotency header, timeouts, GET-only retries | Implemented; **tested with mocked HTTP**; live contract **externally dependent** (HMS key + validation, section 5) |
| OpenAI tool calling, Pydantic validation, confirmation gate, call limits | Implemented; **tested with a scripted fake model**; real-model behaviour **not tested** (needs `OPENAI_API_KEY`) |
| FAQ answers from approved file | Implemented; **sample answers are unverified** |
| Booking / conflict / timeout / duplicate / cancellation flows | Implemented and unit-tested (mocked HMS) |
| Human handoff, callbacks API, staff alerts | Implemented and tested |
| Call summaries | Implemented and tested |
| Reminders (persistent, no duplicates, cancel-aware, restart-safe) | Implemented and tested |
| Email (SMTP) / SMS (Twilio REST) providers | Implemented; **mock tested**, live delivery **externally dependent** |
| Twilio voice webhooks + signature verification | Implemented; signature verified against Twilio's published test vector; **live call not tested** |
| Mock mode (HMS, LLM, telephony, email, SMS) | Working, smoke-tested with uvicorn |
| Rate limiting | Implemented, in-memory (single process) |
| Production DB | Any SQLAlchemy URL via `DATABASE_URL` (e.g. PostgreSQL + `pip install psycopg[binary]`); only SQLite was tested |

## 10. Final integration checklist
- [ ] `.env` created; `INTERNAL_API_KEY` set; `.env` not committed
- [ ] `HMS_API_KEY` from Akhil; contract validated (section 5); conflict/leave/cancel responses checked
- [ ] FAQ file reviewed and `"verified": true` set by hospital staff; real emergency protocol wording agreed
- [ ] `OPENAI_API_KEY` + `OPENAI_MODEL` set; 10+ real test conversations (booking, conflict, cancel, human request) reviewed
- [ ] SMTP / SMS credentials set; a test confirmation, reminder, staff alert and summary received
- [ ] Twilio number, ngrok/HTTPS URL, `PUBLIC_BASE_URL`, both webhooks configured; real test call completed
- [ ] Privacy/legal review (call recording is NOT enabled; patient data retention; DLT/telecom rules)
- [ ] Deploy behind HTTPS with a managed database and a single worker (or an external rate limiter); `APP_ENV=production`

## 11. Deployment notes
Any host that runs Python 3.12 (Render, Railway, Azure App Service, a VM). Start command:
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Use a persistent volume for SQLite or, preferably, PostgreSQL.
Run **one** instance (the reminder loop and rate limiter are per process). Set secrets as platform environment variables.

## 12. Troubleshooting
| Symptom | Fix |
|---|---|
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `ZoneInfoNotFoundError: Asia/Kolkata` | `pip install tzdata` (in requirements) |
| `401` on `/api/...` | send header `X-Internal-Api-Key` equal to `.env` value |
| `503 INTERNAL_API_KEY is not configured` | set it in `.env`, restart |
| Twilio calls get `403` | `PUBLIC_BASE_URL` must exactly match the URL Twilio calls (https, no trailing slash); token correct |
| Bot says "[MOCK MODE]" | `OPENAI_API_KEY` is empty |
| `/health` shows hms `mock` | `HMS_API_KEY` is empty (or `HMS_MOCK_MODE=true`) |
| Booking says outcome unconfirmed | HMS timed out or returned 5xx; staff must check the HMS before re-booking |
| No SMS received | check `/health` sms mode, the `notifications` table `status/error`, Twilio trial/DLT limits |
