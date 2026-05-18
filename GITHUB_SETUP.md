# live-market-scanner — GitHub setup

This is the deployment guide for getting the scanner running in
**https://github.com/aiguy2k25-sketch/live-market-scanner** with scheduled
daily emails to `2daysale@gmail.com`.

## 1. Get the code into the repo

From inside the unzipped project directory:

```bash
git init
git remote add origin https://github.com/aiguy2k25-sketch/live-market-scanner.git
git add .
git commit -m "Initial commit: macro gate + scanner"
git branch -M main
git push -u origin main
```

If the remote already has files, you may need `git pull --rebase origin main`
or force-push with `git push -u origin main --force` (only if you're sure
the remote contents can be discarded).

## 2. Create a Gmail App Password

GitHub Actions can't log into Gmail with your normal password — Google
requires an "App Password" for SMTP.

1. Make sure **2-Step Verification** is on for your Google account.
   https://myaccount.google.com/security
2. Go to **App Passwords**: https://myaccount.google.com/apppasswords
3. Create a new password. Name it `live-market-scanner`. Copy the 16-character
   password Google generates.

## 3. Add GitHub Secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**

Add three secrets:

| Name                  | Value                                       |
|-----------------------|---------------------------------------------|
| `GMAIL_USER`          | the Gmail address you'll send FROM          |
| `GMAIL_APP_PASSWORD`  | the 16-char App Password from step 2        |
| `EMAIL_TO`            | `2daysale@gmail.com`                        |

The sending and receiving address can be the same Gmail if you want — Gmail
allows self-sending.

## 4. Test it manually

Go to **Actions → Daily Scanner → Run workflow**. This runs the full pipeline
once on demand. Watch the logs; you should see a "✅ Email sent" near the end.
Check inbox at `2daysale@gmail.com`.

## 5. Schedule

The workflow is set to run **Mon-Fri at 21:00 UTC** (≈4pm ET during US DST,
≈5pm ET in winter). To change:

Edit `.github/workflows/daily-scan.yml`, the `cron` line. Note that GitHub
Actions cron is always UTC.

Common alternatives:
- `'0 12 * * 1-5'`  → 8am ET pre-market (during DST)
- `'30 21 * * 1-5'` → 4:30pm ET (gives data a chance to settle)
- `'0 23 * * 0'`    → Sunday 7pm ET (weekly)

## 6. What you'll get in the email

- **Subject:** "Scanner Results — YYYY-MM-DD"
- **Body:** macro zone status + top 25 ranked names with all 5 factor scores
- **Attachment:** full ranked CSV with every ticker and raw factor values
- **If macro is DEFENSIVE:** a one-line email saying scanner is disabled.

## Caveats — important

1. **GitHub free-tier cron is best-effort.** It can be delayed 30+ minutes
   during heavy GitHub load. If you need guaranteed timing, run this on a
   small cloud VM with a real cron.
2. **Factor 5 (Short Interest Decline)** uses a level proxy — see
   `scanner/scoring.py` for the explanation. The spec asks for change-over-time
   which needs paid data (FINRA / Sharadar).
3. **Yahoo Finance can rate-limit.** If a run fails with HTTP 429, just rerun
   the workflow. For a paid replacement, swap `signals/data_utils.py`.
4. The Streamlit dashboard is **NOT** deployed by GitHub Actions — it's a
   local app. The Action only runs the CLI scan + email. To run the dashboard,
   `streamlit run run_macro_gate.py` locally.
