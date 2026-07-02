# TechPro CRM — Python (FastAPI) Edition

A from-scratch Python rewrite of the single-file HTML/JS CRM, with real
server-side enforcement instead of client-side checks that could be
bypassed from the browser console. Feature-complete against the
original — see `FEATURE_PARITY.md` for the full checklist and the
handful of deliberate differences.

## What's different from the JS version (and why)

- **PIN check, lockout, and role permissions run on the server.** In the
  JS version these were all client-side JavaScript — anyone with devtools
  could bypass them. Here, the browser is just rendering HTML the server
  decided to send; there's no client-side logic to bypass.
- **No Firebase, no secret to leak.** Data lives in a local SQLite file
  (`crm.db`). Nothing about the database is ever exposed to the browser.
- **PINs are bcrypt-hashed**, never stored or transmitted in plaintext.
- **XSS is handled for you.** Jinja2 escapes all variables by default —
  no manual `escH()`/`escJS()` helpers needed anywhere.
- **Email/SMS send directly from the server.** The original needed a
  Cloudflare Worker proxy for Twilio because browsers can't safely hold
  that secret. Since this has a real backend, SMTP/Twilio credentials
  just live in Settings and the server calls those APIs directly.

## Setup

```bash
cd crm_python
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000` (or `http://<shop-pc-ip>:8000` from
another device on the same WiFi).

Default login: **PIN 1234** (seeded as the Owner). Change this
immediately from the Staff page once you're in.

## Setting up Email & SMS (optional)

Both are optional — invoices/repairs work fine without them, the
Email/SMS buttons just show "not configured" until you add credentials
in **Settings**.

**Email (SMTP):** works with Gmail (use an [App Password](https://myaccount.google.com/apppasswords),
not your real password), or any SMTP provider (SendGrid, Mailgun, your
domain host, etc.). Host/port/username/password/from-address.

**SMS (Twilio):** sign up at twilio.com, get a free trial number, and
grab your Account SID + Auth Token from the console. Paste those plus
the Twilio phone number into Settings.

**Daily Owner Digest:** once SMTP is set up, enable it in Settings →
Daily Owner Digest, set who it goes to and what hour (server-local
time) it sends. There's a "Send Test Digest Now" button so you don't
have to wait until that hour to check it works. Runs via APScheduler,
which starts automatically with the app — no separate cron job needed.

## Project layout

```
app/
  database.py      — SQLAlchemy engine/session setup
  models.py         — ORM models (Staff, Product, Customer, Invoice, Repair, ...)
  auth.py           — PIN hashing, lockout state, role checks
  tax.py            — Canadian sales tax calculator (by province)
  repairs_const.py  — repair status pipeline definitions
  notifications.py  — SMTP email + Twilio SMS sending
  seed.py           — creates tables + seeds an Owner account and sample products
  main.py           — all routes
templates/          — Jinja2 HTML templates (autoescaped by default)
static/style.css    — dark theme matching the original CRM's look
```

## Feature coverage

See `FEATURE_PARITY.md` for the full section-by-section checklist
against the original HTML/JS CRM, including a few small, real gaps
(CSV *import* for inventory, invoice-specific SMS, customer notes
editing) and a short list of deliberate differences worth knowing about
(e.g. what "wipe all data" does and doesn't touch).

## Migrating your existing data

Once you're happy with this, export your current CRM's Firebase data as
JSON and I can write a one-off script that loops through it and inserts
matching rows via these same SQLAlchemy models.

## Notes

- The session secret is generated once and saved to `.session_secret`
  (gitignored) so restarting the server doesn't log everyone out. Set
  the `SESSION_SECRET` environment variable instead if you deploy this
  somewhere with an ephemeral filesystem.
- `crm.db` is your actual data — back it up the same way you'd back up
  any file, or use Settings → Data Export → Download Full Backup.
