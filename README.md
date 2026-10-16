# US Leading Indicators · 美國領先指標

Static [GitHub Pages](https://hiulungchu-art.github.io/us-leading-indicators/) dashboard for HiuLung’s cycle-assistant view of US leading / cycle indicators.

**只供參考，非投資建議。** For reference only — not investment advice.

## Live site

https://hiulungchu-art.github.io/us-leading-indicators/

## What it does

1. **GitHub Actions** (Mon–Fri cron + manual dispatch) runs `scripts/fetch_fred.py`
2. Script writes **`docs/data.json`** (FRED series + rule-based badge + merged `docs/manual.json`)
3. **`docs/index.html`** reads that JSON and renders status badge, cards, Chart.js sparklines

Architecture pattern (FRED → JSON → static Pages + Actions) was studied from open-source projects — this repo’s UI, rules, and series set are **original**. We do **not** re-host their model weights or claim their recession probability.

### Inspiration / credit

- [ianperaltahirujo/recession-probability-dashboard](https://github.com/ianperaltahirujo/recession-probability-dashboard) — weekly Actions → static Pages + FRED
- [ribromeu/US-Yield-Curve-Macro-Dashboard](https://github.com/ribromeu/US-Yield-Curve-Macro-Dashboard) (MIT) — yield curve + HY OAS monitoring patterns

## Rule-based badge（透明規則，非 ML）

Each available signal votes **+1 / 0 / −1**. Sum:

| Score | Status |
|------:|--------|
| ≥ +2 | **偏牛** (lean bullish) |
| ≤ −2 | **偏熊** (lean bearish) |
| else | **過渡** (transitional) |

Signals:

| Signal | +1 (牛) | −1 (熊) | 0 |
|--------|---------|---------|---|
| **2s10s (T10Y2Y)** | > +0.10 pp | < −0.05 pp (inverted) | near flat |
| **ISM Manufacturing PMI** (manual) | ≥ 52 | < 48 | 48–52 |
| **Jobless claims** (IC4WSA/ICSA) | low & stable (&lt;230k, not rising hard) | high or rising sharply | otherwise |
| **HY OAS** | ≤ 3.5% and not widening much | ≥ 5% or widen ≥ 0.5 pp vs ~1m | otherwise |
| **S&P 500** | &gt; +3% vs earlier in spark window | &lt; −5% vs earlier | otherwise |

Full vote detail is shown on the page under the badge.

## Auto-updated (FRED)

| Series | Meaning |
|--------|---------|
| T10Y2Y | 2s10s |
| T10YFF | 10y − Fed Funds |
| DFF | Effective federal funds |
| ICSA / IC4WSA | Initial claims / 4-week avg |
| BAMLH0A0HYM2 | ICE BofA US HY OAS |
| PERMIT / HOUST | Building permits / housing starts |
| UMCSENT | Michigan consumer sentiment (often lags) |
| SP500 | S&P 500 |
| UNRATE / PAYEMS | Unemployment / nonfarm payrolls (context) |

### FRED API key (recommended)

The script works **without** a key via public CSV:

`https://fred.stlouisfed.org/graph/fredgraph.csv?id=SERIES`

For reliability in Actions, add a free key:

1. Create key at https://fredaccount.stlouisfed.org/apikeys  
2. Repo → **Settings → Secrets and variables → Actions** → New secret  
3. Name: `FRED_API_KEY` · Value: your key  

If the secret is missing, Actions still run in keyless CSV mode.

## Manual fields (`docs/manual.json`)

Edit and commit when new releases drop (not free on live FRED):

- Conference Board **LEI** (level, MoM, date)
- **ISM Manufacturing PMI**
- TCB **Consumer Confidence** / **CEO Confidence**

Pre-filled from a 2026-10-16 public snapshot. Next LEI release noted around 2026-10-22 (Sep data).

## Local refresh

```bash
# optional
export FRED_API_KEY=your_key

python3 scripts/fetch_fred.py
# writes docs/data.json
```

No pip packages required (stdlib only).

## Enable GitHub Pages

Settings → Pages → Build from branch **main** / folder **/docs**.

Or via CLI (already used at create time if apply succeeds):

```bash
gh api repos/hiulungchu-art/us-leading-indicators/pages \
  -X POST -f build_type=legacy -f source[branch]=main -f source[path]=/docs
```


## GitHub Actions workflow

The authenticated `gh` token on this box lacks the **`workflow`** OAuth scope, so
`.github/workflows/update.yml` could not be pushed automatically.

**Install once (GitHub web UI):**

1. Open https://github.com/hiulungchu-art/us-leading-indicators/new/main?filename=.github/workflows/update.yml  
2. Paste the contents of [`actions/update.yml`](actions/update.yml)  
3. Commit  

**Or** re-authorize `gh` with `workflow` scope, then:

```bash
mkdir -p .github/workflows
cp actions/update.yml .github/workflows/update.yml
git add .github/workflows/update.yml
git commit -m "Add FRED update workflow"
git push
```

After the workflow exists, optionally add secret `FRED_API_KEY` (see above).

## Disclaimer

Educational / personal dashboard. **只供參考，非投資建議.**
