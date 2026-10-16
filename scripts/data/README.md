# Offline history caches

Used when FRED API/CSV is slow or unavailable during `build_crash_comparisons`:

| File | Series | Notes |
|------|--------|-------|
| `T10Y3M_mirror.csv` | Daily 10y−3m spread | Prefer refresh from eco3min FRED mirror or FRED API |
| `RECPROUSM156N.csv` | Monthly Chauvet–Piger | Prefer FRED API; tip may be refreshed via short curl |

These are **not** invented. Rebuild prefers live FRED/`fred_api` when `FRED_API_KEY` is set.
