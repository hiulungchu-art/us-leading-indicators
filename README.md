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

### 本站規則評估 · 衰退／急挫風險%（透明映射，非 ML）

Same votes as the badge, remapped to a **0–100** risk percentage:

| 每票 vote | 風險貢獻 |
|----------:|--------:|
| **+1**（偏牛） | **0%** |
| **0**（中性） | **50%** |
| **−1**（偏熊） | **100%** |

等權平均，等價閉式：

```text
risk_raw = 100 × (votes_cast − score) / (2 × votes_cast)
risk     = clamp(risk_raw, 5, 95)
```

Caps **5–95** avoid implying false certainty at 0% or 100%. This is **not** a calibrated probability and **not** the NY Fed model.

### 公開衰退概率（NY Fed 等）

Displayed **side-by-side** with the site rule risk:

| Model | What it is | Source |
|-------|------------|--------|
| **NY Fed**（主） | 12-month-ahead recession probability from **10y−3m** Treasury spread (published probit; α=−0.5333, β=−0.6330, sample 1959–2009) | [NY Fed ycfaq](https://www.newyorkfed.org/research/capital_markets/ycfaq) · [Prob_Rec.pdf](https://www.newyorkfed.org/medialibrary/media/research/capital_markets/Prob_Rec.pdf) · spread via FRED `T10Y3M` |
| **Chauvet–Piger**（次要） | Smoothed **current** recession probability | FRED [`RECPROUSM156N`](https://fred.stlouisfed.org/series/RECPROUSM156N) |

`scripts/fetch_fred.py` writes these into `docs/data.json` (`public_probs`, `site_risk`) and `docs/public_probs.json`. Official NY Fed **CSV/XLS** was not available at refresh time (404); values come from the official PDF chart and/or the same published parameters applied to FRED `T10Y3M` monthly averages — **not invented**.

### 而家 vs 大股災／衰退前（公開模型對照）

Under the probability cards, the page shows a dated comparison table:

| Column | Content |
|--------|---------|
| **NY Fed** | Same published α/β × FRED `T10Y3M` monthly avg at anchors ~6–12m before equity peaks / NBER starts (Dot-com, GFC, COVID; optional 1990). 「而家」uses the live official print when available. 1987 is labeled **retrospective** (Black Monday was not an NBER recession; modern chart publication came later). |
| **Chauvet–Piger** | FRED `RECPROUSM156N` at the same anchors; secondary rows show the series **rising into** recessions (nowcast, not 12m-ahead). |
| **本站規則評估** | **Not** in the crash table — it is a current vote mapping, **not** historically calibrated / backtested crash %. |

Anchors and values live in `docs/public_probs.json` → `crash_comparisons` (also embedded in `docs/data.json`). Offline fallbacks for history rebuilds: `scripts/data/T10Y3M_mirror.csv`, `scripts/data/RECPROUSM156N.csv`.


**Disclaimer:** 公開模型 ≠ 本站預測；兩者都 **非投資建議**.

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

## Historical reference (`docs/history_ref.json`)

Long-run normals (mean / median / p10–p90, trailing ~10y when available) and crash-episode snapshots
(1987, 2000–02, 2007–09, 2020) for key FRED series. Rendered on the Pages site under **參考**.

Rebuild:

```bash
# CSV keyless preferred when reachable; else API key
export FRED_API_KEY=your_key   # optional
python3 scripts/build_history_ref.py
# writes docs/history_ref.json
```

**Gaps:** FRED HY OAS (`BAMLH0A0HYM2`) keeps ~3y history (as of 2026); FRED `SP500` keeps ~10y. Older credit stress uses `BAA10Y`. No invented prints.

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
