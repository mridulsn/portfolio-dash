# Portfolio & Market Dashboard (auto-updating, encrypted)

A private, password-protected dashboard for an Indian stocks + mutual-fund portfolio.
Rebuilt every 5 minutes during NSE market hours by GitHub Actions and deployed to Vercel.

## How it works
- `build.py` screens the Nifty 500 (momentum + risk + trend composite), screens mutual funds
  (AMFI/mfapi history), and reads the personal holdings panel.
- The holdings payload is **AES-256-GCM encrypted** (PBKDF2-SHA256) before being embedded in
  `template.html`, so the published file is unreadable without the password. Safe to host publicly.
- `.github/workflows/refresh.yml` runs the build on a 5-min cron and deploys `public/index.html`.

## Data freshness
Free EOD / ~15-min-delayed price data (yfinance). MF NAVs are end-of-day (AMFI). Not real-time ticks.

## Required GitHub repo secrets
| Secret | What it is |
|---|---|
| `DASH_PASSWORD` | password that unlocks the dashboard |
| `HOLDINGS_JSON` | `{"asof":"YYYY-MM-DD","stocks":[[sym,sector,qty,avg,lt_qty,isin],...],"mf":[[name,isin,cat,units,avg],...],"sips":[[isin,amount,day,name],...]}` - generate it with `import_kite.py` |
| `VERCEL_TOKEN` | Vercel access token |
| `VERCEL_ORG_ID` | from `.vercel/project.json` after `vercel link` |
| `VERCEL_PROJECT_ID` | from `.vercel/project.json` after `vercel link` |

## Updating holdings
Download the holdings statement from Kite Console and run
`python import_kite.py "path\to\holdings.xlsx" --push`. It rewrites `holdings.local.json`, updates the
`HOLDINGS_JSON` secret and starts a refresh. The `sips` list is kept.

## SIPs update themselves
Every instalment dated after the statement's `asof` date is added at the real NAV of that day
(less 0.005% stamp duty) once the NAV is published. A new statement resets the baseline, so nothing
is double-counted. Edit the `sips` list only when you start, stop or change a SIP.

## Local test
`python build.py --from-cache` re-renders the page from `last_payload.json` without downloading anything.
```
DASH_PASSWORD=yourpass python build.py   # writes public/index.html
```
> Holdings live only in `HOLDINGS_JSON` (a secret) — never in this source. EOD/delayed data, not advice.
