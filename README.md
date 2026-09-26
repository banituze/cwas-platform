# CWAS: Community Water Access Scheduler

A role-based booking and water-management system for communities that depend on shared water sources such as wells, boreholes, and public taps. Household members can register, view available time slots, book and pay for water collection, and receive receipts and notifications. Community coordinators manage water sources, review and approve bookings, schedule maintenance, and generate reports. Administrators manage user accounts, oversee financial records, configure the system, and maintain the database.

One Flask app serves the website, the household and coordinator apps, the administrator console, and the USSD (`*384*9411#`) and SMS (`7380`) channels.

## Run it

These steps take a fresh computer to a running CWAS. Type each command in a terminal: Terminal on macOS and Linux, PowerShell on Windows.

**1. Install the tools.** Python 3.12 (3.10 or newer works) and Git. Check them with `python3 --version` (on Windows `py --version`) and `git --version`. Node.js is not needed to run CWAS; it is only used to rebuild the stylesheet after a style change (see [Change the styles](#change-the-styles)).

**2. Get the code.**

```bash
git clone https://github.com/banituze/cwas-platform.git
cd cwas-platform
```

**3. Create a virtual environment**, so CWAS's packages stay apart from the rest of the computer.

macOS and Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

The prompt now starts with `(.venv)`. Activate it again in every new terminal. If PowerShell refuses to run the script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once and try again.

**4. Install the packages.**

```bash
pip install -r requirements.txt
```

**5. Start the server.**

```bash
python3 app.py
```

On Windows use `python app.py`. Keep this terminal open; the server runs until you press Ctrl+C.

**6. Open http://localhost:5000.** On the first start CWAS creates the SQLite database in `instance/cwas.db`, seeds a super administrator and loads demo data, so every screen has something to show. No settings are needed for a local run.

**7. Sign in** with one of these accounts:

| Who | Sign in | Notes |
|---|---|---|
| Demo coordinator | `coordinator@cwas.demo` or `+261340000001` / `Demo Water @2026` | USSD PIN `2468` |
| Demo households | `+261340000101` to `+261340000108` / `Demo Water @2026` | USSD PIN `1234` |

**8. Try the phone channels.** Open http://localhost:5000/simulator, pick a demo phone, dial `*384*9411#` or text `BALANCE` to `7380`. The simulator drives the real USSD and SMS code; messages are recorded instead of sent until Africa's Talking keys are set.

**9. Run the tests** (optional). They use a throwaway database and take about 20 seconds.

```bash
python3 -m unittest tests.test_platform
```

**Start again from a clean database:** stop the server, delete the `instance` folder, start it again.

**If something goes wrong:**

| Problem | Fix |
|---|---|
| `python3` is not found on Windows | Use `py` or `python` instead. |
| `Address already in use` | Another program uses port 5000. Start on another port: `PORT=5050 python3 app.py` (PowerShell: `$env:PORT=5050; python app.py`). |
| `pip install` fails | Upgrade pip with `python3 -m pip install --upgrade pip`, then run step 4 again. |
| Pages have no styles | `static/css/app.css` ships with the code. If it was deleted, rebuild it as in [Change the styles](#change-the-styles). |
| Signed out after every restart | Set a fixed `SECRET_KEY` (see [Configuration](#configuration)); without it a new key is made in `instance/`. |

## What is in it

* **Website**: landing page with an entrance orbit, a scroll-driven gallery tunnel, liquid type and hover reveals; platform, access, water points, about; **Terms of Service, Privacy Policy and Refund Policy** in three languages; footer language switch.
* **Household app**: booking with a live slot grid and a photograph of water poured at a well and real **20 L jerrycans** that fill with the litres chosen; wallet; bookings with a **receipt preview** (metal thermal printer animation) without leaving the page; notifications with live toasts and a chime; profile; **download my data** and **delete my account** (personal data removed, any balance recorded as a refund due).
* **Assistant**: saved chats (rename, export, delete one or all), **file attachments of any type** (images, PDF, text, CSV, JSON, Office files, archives, audio, video, programs are stored safely and described honestly), **voice input**, **read replies aloud** (each a toggle), copy, sounds.
* **Coordinator console**: approval queue, bookings, water points, maintenance, households, deposits, reports with CSV, AI insights, announcements.
* **Administrator console**: users and roles, settings (prices, subsidies, **enrollment codes for USSD**), audit log with chain verification, backup and restore, channel logs.
* **Registration** asks the same questions on the web, by USSD and when a coordinator registers a household: household size, needs (tick boxes), distance to water and the water point used most. Self-reported needs count for priority at once; the subsidy waits for a coordinator's check.
* **Pilot news**: an email-only "Follow the pilot" list in the footer, with a one-click leave link and an export in Admin > overview.
* **Themes**: Flag, the default, puts the three flag colours together: a white page on a light flag field, deep-green text, green buttons and a red call to action. White, Green and Red each dominate their theme (page, buttons, logo tile, favicon, app icons, phone and printer covers). Every glass surface uses one liquid-glass recipe.
* **Demo phones** on the homepage and `/access` are the device lab's own handsets (`static/js/sim.js`), playing screens rendered by the USSD engine (`ussd.demo_script`), so they match a real call exactly.
* **Device lab** (`/simulator`): three Winebald handsets (Lite 2 keypad phone, Nova 6, Max 9 Pro) with front and back, physical buttons, lock screen, and the Phone, Messages, Contacts and CWAS apps in the dock (the CWAS app opens the dialer with the service code ready), synthesised sounds, incoming SMS banners, guided runs and a live **network console** showing each request to `/api/ussd`.
* **USSD and SMS**: Service code `*384*9411#`, shortcode `7380`. 

## Phones without data
Dial `*384*9411#`: pick a language, and the menu opens with your first name. The PIN is asked only right before something that moves money or changes the account, and `0` at the PIN prompt starts Forgot PIN. Everyone sets a 6-digit recovery code at registration; without it, `PIN HELP` to 7380 asks a coordinator to call back, check your details and send a temporary PIN by SMS. SMS is kept for what matters: welcome, deposits, booking decisions, maintenance cancellations, PIN changes and announcements. 

## Configuration

Everything is optional. Copy `.env.example` to `.env` for local use, or set variables on the host.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL URL. Empty means SQLite. If PostgreSQL cannot be reached at start-up the app logs it and falls back to SQLite. |
| `SECRET_KEY` | Session signing key. If empty one is generated once in `instance/secret.key`. |
| `SITE_URL` | Public address, for example `https://cwas.winebald.tech. Used for canonical links, the sitemap and link previews. Without it, the address of each request is used. |
| `SECURITY_CONTACT` | Email or URL published in `/.well-known/security.txt` for vulnerability reports. Defaults to `ADMIN_EMAIL`. |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | First administrator, used only when none exists. Password change is forced at first sign-in. |
| `SEED_DEMO`, `SIMULATOR_PUBLIC` | Override the demo-data and device-lab defaults. |
| `SMS_ENABLED`, `AT_USERNAME`, `AT_API_KEY`, `AT_SENDER_ID`, `AT_USSD_CODE`, `AT_SHORTCODE` | Africa's Talking. With `SMS_ENABLED=0` SMS is recorded as simulated. |
| `AT_WEBHOOK_TOKEN` | Shared token for the telco callbacks. Set it before going live. |
| `PAYMENT_MODE` | `simulation` posts deposits at once (default locally). `live` keeps them pending until the provider or a coordinator confirms (default in production, so money can never be minted by accident). |
| `PAYMENT_WEBHOOK_SECRET` | HMAC-SHA256 secret for `/webhooks/payments/<orange|airtel>`. Without it no deposit is confirmed by webhook. |
| `SMTP_*` | Optional email for password resets. SMS is used when the account has a phone. |

The coordinator enrollment code lives in **Admin > Settings**. Administrators are created on the web, in **Admin > Users**.

## Search engines and security

* **Search engines**: each public page has its own title and description, a canonical address, `hreflang` links for Malagasy, French and English (`?lang=mg`, `?lang=fr`), Open Graph and Twitter cards with a 1200 x 630 share image, and schema.org data (Organization, WebSite, WebPage, and the homepage FAQ). `/robots.txt` and `/sitemap.xml` are generated; accounts, consoles and APIs are `noindex` and never cached.
* **Security**: strict Content Security Policy (scripts only from this site, no framing), HSTS in production, a `__Host-` session cookie, same-origin resource policy, `no-store` on everything personal, rate limits, account and PIN lockouts, CSRF on every form, payment webhooks that must be signed and must confirm the exact amount, and telco callbacks refused in production until `AT_WEBHOOK_TOKEN` is set. Report a vulnerability through `/.well-known/security.txt`.
* **Compliance seals** in the footer name the frameworks CWAS is built to follow.

## How it is built

```
app.py         app factory, security headers, DB fallback, seeding, waitress entry point
extensions.py  shared Flask extensions and the single-writer lock
models.py      SQLAlchemy schema (users, households, water sources, bookings, wallet ledger, audit chain, chats, ...)
services.py    every business rule: slots, pricing, wallet, booking lifecycle, events, AI checks, reports
ussd.py        USSD menu engine and SMS commands (same services as the web)
uploads.py     chat attachments: detect, store, describe
legal.py       Terms, Privacy, Refund texts (English source)
web.py         routes for the site, apps, consoles, webhooks, assistant and simulator
i18n.py, translations.py   Malagasy / French / English
templates/, static/        Jinja templates; Tailwind compiled to static/css/app.css (committed)
static/js/     app.js (sound, toasts, receipts, video) · fx.js (motion) · assistant.js · sim.js (device lab) · boot.js
tests/, tools/             end-to-end tests; translation gap finder
```

* Money: wallet balance is a cached sum of a signed ledger. Every booking debit, refund and deposit is one transaction, a refund is unique per booking (database constraint), and `reconcile_wallet` checks the cache against the ledger.
* Slots: a partial unique index allows one active booking per household per day. Capacity and maintenance are rechecked at confirmation.
* Audit: each entry stores the hash of the previous one, so an edited or deleted row breaks the chain and the admin console shows where.
* AI : forecasting, priority scoring, risk suggestions, anomaly detection, fairness comparison, conflict alternatives and the assistant are transparent rules over your own data. They are not trained models; that is on purpose, so every decision can be explained.
* Uploads are stored outside the web root, named at random, never executed, and served as downloads with a sandboxing CSP (only verified images and PDFs open inline).
* Offline: the service worker caches static files and pages already opened under `/app`. Bookings and payments need a connection (or USSD).

### Change the styles
Tailwind is compiled at build time and the result is committed, so running the app never needs Node.

```bash
npm install
npm run build:css
```

## Credits

Video and photo assets are from Pexels and used under the Pexels licence. Thanks to the creators: B. Aristotlè Guweh Jr, Thirty Story (two clips) and manas patra; the two photographs are by their Pexels photographers, credited on the Pexels pages linked from the asset URLs in `templates/partials/media.html`.
Fonts: Bricolage Grotesque and Figtree (SIL Open Font Licence) via Fontsource. Payment logos belong to Orange and Airtel. The motion effects are original code written for this project, inspired by interaction ideas catalogued on HorizonX.

## Author

[Winebald Banituze](https://github.com/banituze)
