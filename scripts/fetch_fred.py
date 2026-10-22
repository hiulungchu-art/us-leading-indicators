#!/usr/bin/env python3
"""
Fetch US leading-indicator series from FRED → docs/data.json.

Supports:
  1) FRED API when FRED_API_KEY is set (preferred, more reliable)
  2) Keyless CSV download from fred.stlouisfed.org/graph/fredgraph.csv?id=SERIES

Merges docs/manual.json (Conference Board LEI, ISM PMI, confidence) into output.
Computes a transparent rule-based cycle badge (偏牛 / 偏熊 / 過渡).
"""
from __future__ import annotations

import json
import math
import re
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MANUAL_PATH = DOCS / "manual.json"
OUT_PATH = DOCS / "data.json"
PUBLIC_PROBS_PATH = DOCS / "public_probs.json"
DATA_DIR = Path(__file__).resolve().parent / "data"

# NY Fed Treasury term-spread model (Estrella–Mishkin / NY Fed published chart).
# Parameters from Prob_Rec.pdf footnote (est. Jan 1959–Dec 2009).
NYFED_ALPHA = -0.5333
NYFED_BETA = -0.6330
NYFED_PAGE = "https://www.newyorkfed.org/research/capital_markets/ycfaq"
NYFED_PDF = "https://www.newyorkfed.org/medialibrary/media/research/capital_markets/Prob_Rec.pdf"

# Approx. history windows for sparklines (calendar days of lookback)
SERIES = {
    "T10Y2Y": {"label_zh": "2s10s 孳息曲線", "label_en": "2s10s yield curve", "unit": "pp", "lookback_days": 120, "freq": "d"},
    "T10YFF": {"label_zh": "10年−聯邦基金", "label_en": "10y−Fed Funds", "unit": "pp", "lookback_days": 120, "freq": "d"},
    "DFF": {"label_zh": "有效聯邦基金利率", "label_en": "Effective Fed Funds", "unit": "%", "lookback_days": 120, "freq": "d"},
    "ICSA": {"label_zh": "初領失業救濟", "label_en": "Initial jobless claims", "unit": "claims", "lookback_days": 180, "freq": "w"},
    "IC4WSA": {"label_zh": "初領4週平均", "label_en": "Claims 4-week avg", "unit": "claims", "lookback_days": 180, "freq": "w"},
    "BAMLH0A0HYM2": {"label_zh": "美高收益OAS", "label_en": "US HY OAS", "unit": "%", "lookback_days": 120, "freq": "d"},
    "PERMIT": {"label_zh": "建築許可", "label_en": "Building permits", "unit": "SAAR thou", "lookback_days": 730, "freq": "m"},
    "HOUST": {"label_zh": "房屋開工", "label_en": "Housing starts", "unit": "SAAR thou", "lookback_days": 730, "freq": "m"},
    "UMCSENT": {"label_zh": "密歇根消費者信心", "label_en": "Michigan sentiment", "unit": "index", "lookback_days": 730, "freq": "m"},
    "SP500": {"label_zh": "標普500", "label_en": "S&P 500", "unit": "index", "lookback_days": 180, "freq": "d"},
    "UNRATE": {"label_zh": "失業率", "label_en": "Unemployment rate", "unit": "%", "lookback_days": 730, "freq": "m"},
    "PAYEMS": {"label_zh": "非農就業", "label_en": "Nonfarm payrolls", "unit": "thou", "lookback_days": 730, "freq": "m"},
}

HKT = timezone(timedelta(hours=8))


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_re_ym = re.compile(r"^\d{4}-\d{2}$")
USER_AGENT = "us-leading-indicators/1.0 (GitHub Pages; educational; +https://github.com/hiulungchu-art/us-leading-indicators)"

# Set after first confirmed FRED HTML/maintenance response; skip further FRED hits in this run.
_FRED_MAINTENANCE = False


# Sanity ranges: reject HTML-parse artifacts (e.g. CSS "400") and other garbage.
VALUE_RANGES: dict[str, tuple[float, float]] = {
    "T10Y2Y": (-10.0, 10.0),
    "T10YFF": (-15.0, 15.0),
    "T10Y3M": (-15.0, 15.0),
    "DFF": (0.0, 30.0),
    "ICSA": (50_000.0, 2_000_000.0),
    "IC4WSA": (50_000.0, 2_000_000.0),
    "BAMLH0A0HYM2": (0.0, 30.0),
    "PERMIT": (200.0, 5000.0),
    "HOUST": (200.0, 5000.0),
    "UMCSENT": (20.0, 120.0),
    "SP500": (100.0, 50_000.0),
    "UNRATE": (0.0, 30.0),
    "PAYEMS": (50_000.0, 300_000.0),
    "RECPROUSM156N": (0.0, 100.0),
}


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:300].lower()
    return (
        head.startswith("<!doctype")
        or head.startswith("<html")
        or "<html" in head
        or "site maintenance" in head
        or "fonts.googleapis.com" in head
    )


def _http_get(url: str, timeout: int = 60) -> tuple[bytes, str]:
    """GET bytes + content-type. Raises if body looks like an HTML error/maintenance page."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv,application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        raw = resp.read()
    # Soft content-type check: FRED CSV sometimes omits type; always sniff body.
    text_head = raw[:800].decode("utf-8", errors="replace")
    if "text/html" in ctype or _looks_like_html(text_head):
        raise RuntimeError(f"HTML response (not data) from {url.split('?', 1)[0]} ctype={ctype!r}")
    return raw, ctype


def _parse_fred_csv_text(series_id: str, raw: str) -> list[tuple[str, float]]:
    """Parse FRED-style CSV; require DATE/observation_date header and YYYY-MM-DD dates."""
    if _looks_like_html(raw):
        raise RuntimeError(f"{series_id}: got HTML instead of CSV")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"{series_id}: empty CSV")
    header = lines[0].lower()
    # Real FRED CSV starts with observation_date,<id> or DATE,<id>
    if not (
        header.startswith("observation_date")
        or header.startswith("date,")
        or header.startswith('"observation_date"')
        or header.startswith('"date"')
    ):
        raise RuntimeError(f"{series_id}: bad CSV header {lines[0][:80]!r}")
    if series_id.upper() not in lines[0].upper() and "DATE" not in lines[0].upper() and "observation_date" not in header:
        # header may be DATE,VALUE without series id — still OK if date-like
        pass
    out: list[tuple[str, float]] = []
    lo_hi = VALUE_RANGES.get(series_id)
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        d, v = parts[0].strip().strip('"'), parts[1].strip().strip('"')
        if not DATE_RE.match(d):
            continue  # skip non-date rows (never treat HTML fragments as dates)
        if not v or v.upper() == ".":
            continue
        try:
            fv = float(v)
        except ValueError:
            continue
        if lo_hi is not None and not (lo_hi[0] <= fv <= lo_hi[1]):
            continue
        out.append((d, fv))
    if not out:
        raise RuntimeError(f"{series_id}: no valid numeric observations in CSV")
    return out


def _validate_points(series_id: str, points: list[tuple[str, float]]) -> list[tuple[str, float]]:
    lo_hi = VALUE_RANGES.get(series_id)
    out: list[tuple[str, float]] = []
    for d, v in points:
        if not DATE_RE.match(d):
            continue
        if lo_hi is not None and not (lo_hi[0] <= v <= lo_hi[1]):
            continue
        out.append((d, v))
    if not out:
        raise RuntimeError(f"{series_id}: all observations failed sanity checks")
    return out


def clamp_recession_pct(value: float, label: str = "recession_pct") -> float:
    """Recession probabilities must be 0–100. Never allow HTML '400' artifacts through."""
    v = float(value)
    if not (0.0 <= v <= 100.0) or math.isnan(v) or math.isinf(v):
        raise RuntimeError(f"{label}: value {v!r} outside 0–100")
    return v


def fetch_csv_series(series_id: str, start: str | None = None) -> list[tuple[str, float]]:
    """Keyless FRED graph CSV (+ mirror). Returns [(date_iso, value), ...] oldest→newest."""
    global _FRED_MAINTENANCE
    if _FRED_MAINTENANCE:
        raise RuntimeError(f"{series_id}: skipped — FRED site maintenance this run")
    params = {"id": series_id}
    if start:
        params["cosd"] = start
    qs = urllib.parse.urlencode(params)
    urls = [
        "https://fred.stlouisfed.org/graph/fredgraph.csv?" + qs,
        "https://fred.mino.mobi/?" + qs,
    ]
    errors: list[str] = []
    for url in urls:
        try:
            raw, _ctype = _http_get(url, timeout=12)
            return _parse_fred_csv_text(series_id, raw.decode("utf-8", errors="replace"))
        except Exception as e:
            err = str(e)
            errors.append(f"{url.split('?',1)[0]}: {e}")
            if "HTML response" in err or "maintenance" in err.lower():
                _FRED_MAINTENANCE = True
                # Don't burn minutes on curl/mino once FRED itself is in maintenance.
                raise RuntimeError(f"{series_id}: CSV fetch failed — " + " | ".join(errors)) from e
    # curl --http1.1 sometimes bypasses flaky stacks (only when not clearly maintenance)
    try:
        return _curl_fred_csv(series_id, start=start, timeout=12)
    except Exception as e:
        errors.append(f"curl: {e}")
    raise RuntimeError(f"{series_id}: CSV fetch failed — " + " | ".join(errors))


def fetch_api_series(series_id: str, api_key: str, start: str | None = None) -> list[tuple[str, float]]:
    global _FRED_MAINTENANCE
    if _FRED_MAINTENANCE:
        raise RuntimeError(f"{series_id}: skipped — FRED site maintenance this run")
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "asc",
    }
    if start:
        params["observation_start"] = start
    url = "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(params)
    raw, ctype = _http_get(url)
    text = raw.decode("utf-8", errors="replace")
    if _looks_like_html(text) or "text/html" in ctype:
        _FRED_MAINTENANCE = True
        raise RuntimeError(f"{series_id}: API returned HTML (maintenance?)")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{series_id}: API JSON parse failed: {e}") from e
    if "error_code" in data or "error_message" in data:
        raise RuntimeError(f"{series_id}: API error {data.get('error_code')} {data.get('error_message')}")
    obs = data.get("observations") or []
    out: list[tuple[str, float]] = []
    lo_hi = VALUE_RANGES.get(series_id)
    for o in obs:
        v = (o.get("value") or "").strip()
        d = (o.get("date") or "").strip()
        if not DATE_RE.match(d):
            continue
        if not v or v == ".":
            continue
        try:
            fv = float(v)
        except ValueError:
            continue
        if lo_hi is not None and not (lo_hi[0] <= fv <= lo_hi[1]):
            continue
        out.append((d, fv))
    if not out:
        raise RuntimeError(f"{series_id}: API returned no valid observations")
    return out


def start_for(lookback_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=lookback_days + 14)).strftime("%Y-%m-%d")


def sparkline(points: list[tuple[str, float]], max_n: int = 60) -> list[dict[str, Any]]:
    if len(points) <= max_n:
        sel = points
    else:
        step = len(points) / max_n
        sel = [points[int(i * step)] for i in range(max_n - 1)] + [points[-1]]
    return [{"d": d, "v": round(v, 4)} for d, v in sel]


def trend_label(points: list[tuple[str, float]], series_id: str) -> str:
    """Short zh trend phrase from recent change."""
    if len(points) < 2:
        return "資料不足"
    latest = points[-1][1]
    # Compare to ~1 month ago for daily, ~3 obs for weekly, ~2 for monthly
    if series_id in ("ICSA", "IC4WSA"):
        ref_idx = max(0, len(points) - 5)
    elif series_id in ("PERMIT", "HOUST", "UMCSENT", "UNRATE", "PAYEMS"):
        ref_idx = max(0, len(points) - 3)
    else:
        ref_idx = max(0, len(points) - 22)
    ref = points[ref_idx][1]
    delta = latest - ref
    eps = abs(ref) * 0.002 if ref else 0.01
    if series_id in ("ICSA", "IC4WSA", "BAMLH0A0HYM2", "UNRATE"):
        # higher = worse for cycle
        if delta > max(eps, 0.02 if series_id == "BAMLH0A0HYM2" else 1000 if "ICS" in series_id else 0.05):
            return "惡化 ↑"
        if delta < -max(eps, 0.02 if series_id == "BAMLH0A0HYM2" else 1000 if "ICS" in series_id else 0.05):
            return "改善 ↓"
        return "大致平穩"
    if series_id in ("T10Y2Y", "T10YFF"):
        if latest < 0:
            return "倒掛" if delta <= 0 else "仍倒掛但收窄"
        if delta < -0.05:
            return "走平／收窄"
        if delta > 0.05:
            return "走陡"
        return "正斜率穩定"
    if series_id == "DFF":
        if abs(delta) < 0.05:
            return "大致持平"
        return "上升" if delta > 0 else "下降"
    if series_id == "PAYEMS":
        # MoM change of level is not great; show direction of last vs prior
        if delta > 50:
            return "就業增加"
        if delta < -50:
            return "就業減少"
        return "變化不大"
    # higher better: SP500, PERMIT, HOUST, UMCSENT
    if delta > max(eps, abs(ref) * 0.01):
        return "上升 ↑"
    if delta < -max(eps, abs(ref) * 0.01):
        return "回落 ↓"
    return "大致平穩"


def format_value(series_id: str, v: float) -> str:
    if series_id in ("ICSA", "IC4WSA"):
        return f"{int(round(v)):,}"
    if series_id in ("PERMIT", "HOUST", "PAYEMS"):
        return f"{v:,.0f}"
    if series_id == "SP500":
        return f"{v:,.2f}"
    if series_id in ("T10Y2Y", "T10YFF", "DFF", "BAMLH0A0HYM2", "UNRATE"):
        return f"{v:.2f}"
    return f"{v:.1f}"


def load_manual() -> dict[str, Any]:
    if not MANUAL_PATH.exists():
        return {"indicators": {}, "notes": ""}
    with MANUAL_PATH.open(encoding="utf-8") as f:
        return json.load(f)



def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _monthly_avg_last_complete(points: list[tuple[str, float]]) -> tuple[str, float] | None:
    """Latest calendar month with at least one obs that is not the current incomplete month when possible."""
    from collections import defaultdict

    by_m: dict[str, list[float]] = defaultdict(list)
    for d, v in points:
        by_m[d[:7]].append(v)
    if not by_m:
        return None
    months = sorted(by_m.keys())
    now_ym = datetime.now(timezone.utc).strftime("%Y-%m")
    # Prefer last fully closed month when current month has data too
    if months[-1] == now_ym and len(months) >= 2:
        ym = months[-2]
    else:
        ym = months[-1]
    avg = sum(by_m[ym]) / len(by_m[ym])
    return ym, avg


def parse_nyfed_pdf() -> dict[str, Any] | None:
    """Best-effort parse of official Prob_Rec.pdf for printed probability + as-of."""
    try:
        raw, _ctype = _http_get(NYFED_PDF, timeout=45)
    except Exception as e:
        print(f"  NY Fed PDF fetch failed: {e}", file=sys.stderr)
        return None
    # Prefer pdftotext if present
    txt = ""
    try:
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(raw)
            pdf_path = f.name
        r = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True,
            timeout=30,
        )
        Path(pdf_path).unlink(missing_ok=True)
        if r.returncode == 0:
            txt = r.stdout.decode("utf-8", errors="replace")
    except Exception:
        txt = ""
    if not txt:
        # crude binary extract of printable runs
        txt = re.sub(rb"[^\x20-\x7e\n]+", b" ", raw).decode("ascii", errors="ignore")

    # e.g. "Sep 2027 = 12.7587%"  (horizon label year) and "through Sep 2026" / "Updated 04-Oct-2026"
    prob_m = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*%",
        txt,
    )
    # data-through month often appears near "data through"
    through_m = re.search(
        r"data through\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})",
        txt,
        re.I,
    )
    updated_m = re.search(
        r"Updated\s+(\d{1,2})-([A-Za-z]{3})-(\d{4})",
        txt,
    )
    spread_m = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})\s*=\s*(-?[0-9]+(?:\.[0-9]+)?)\s*(?!\s*%)",
        txt,
    )
    if not prob_m:
        return None
    mon = {
        "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
        "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
    }
    # Probability chart labels the *horizon* month (data month + 12m)
    horizon_mon, horizon_year, prob_s = prob_m.group(1), prob_m.group(2), prob_m.group(3)
    value_pct = clamp_recession_pct(round(float(prob_s), 4), "nyfed_pdf")
    if through_m:
        data_ym = f"{through_m.group(2)}-{mon[through_m.group(1)]}"
    else:
        # infer: horizon − 12 months
        hy, hm = int(horizon_year), int(mon[horizon_mon])
        hm -= 12
        while hm <= 0:
            hm += 12
            hy -= 1
        data_ym = f"{hy}-{hm:02d}"
    updated = None
    if updated_m:
        um = {k.lower(): v for k, v in {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
            "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }.items()}
        updated = f"{updated_m.group(3)}-{um[updated_m.group(2).lower()]}-{int(updated_m.group(1)):02d}"
    spread = float(spread_m.group(3)) if spread_m else None
    return {
        "value_pct": value_pct,
        "date": data_ym,
        "horizon_label": f"{horizon_mon} {horizon_year}",
        "updated_on_source": updated,
        "spread_pp": spread,
        "fetch_method": "nyfed_pdf",
    }


def compute_nyfed_from_fred(api_key: str | None) -> dict[str, Any]:
    """Apply published NY Fed probit to FRED T10Y3M monthly average (not our invention)."""
    start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d")
    points = None
    src = "fred"
    errors: list[str] = []
    if api_key:
        try:
            points = fetch_api_series("T10Y3M", api_key, start=start)
            src = "fred_api"
        except Exception as e:
            errors.append(f"api:{e}")
    if points is None:
        try:
            points = fetch_csv_series("T10Y3M", start=start)
            src = "fred_csv"
        except Exception as e:
            errors.append(f"csv:{e}")
    if points is None:
        try:
            points = fetch_h15_treasury_spreads()["T10Y3M"]
            src = "fed_h15_derived"
        except Exception as e:
            errors.append(f"h15:{e}")
    if points is None:
        # eco3min / local mirror via history loader
        try:
            monthly, tag = _load_t10y3m_history(api_key)
            # synthesize points from monthly averages (mid-month date)
            points = [(f"{ym}-15", v) for ym, v in sorted(monthly.items()) if ym >= start[:7]]
            src = tag
        except Exception as e:
            errors.append(f"mirror:{e}")
    if not points:
        raise RuntimeError("T10Y3M unavailable: " + "; ".join(errors))
    points = _validate_points("T10Y3M", points)
    got = _monthly_avg_last_complete(points)
    if not got:
        raise RuntimeError("T10Y3M: no monthly average")
    ym, spread = got
    z = NYFED_ALPHA + NYFED_BETA * spread
    value_pct = round(_norm_cdf(z) * 100.0, 4)
    clamp_recession_pct(value_pct, "nyfed_probit")
    # horizon = data month + 12m
    y, m = int(ym[:4]), int(ym[5:7])
    m += 12
    while m > 12:
        m -= 12
        y += 1
    months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return {
        "value_pct": value_pct,
        "date": ym,
        "horizon_label": f"{months[m]} {y}",
        "spread_pp": round(spread, 5),
        "updated_on_source": None,
        "fetch_method": f"nyfed_probit_t10y3m:{src}",
        "fred_spread_id": "T10Y3M",
        "parameters": {"alpha": NYFED_ALPHA, "beta": NYFED_BETA},
    }


def fetch_ny_fed_prob(api_key: str | None) -> dict[str, Any]:
    """
    Primary public model: NY Fed 12-month-ahead recession probability from 10y−3m spread.
    Prefer official PDF print; fall back to same published formula on FRED/mirror T10Y3M.
    PDF alone is enough when FRED is in maintenance.
    """
    computed = None
    computed_err = None
    try:
        computed = compute_nyfed_from_fred(api_key)
        clamp_recession_pct(computed["value_pct"], "nyfed_probit")
    except Exception as e:
        computed_err = str(e)
        print(f"  NY Fed probit/FRED unavailable: {e}", file=sys.stderr)

    pdf = None
    try:
        pdf = parse_nyfed_pdf()
    except Exception as e:
        print(f"  NY Fed PDF parse error: {e}", file=sys.stderr)

    if pdf is None and computed is None:
        raise RuntimeError(
            "ny_fed_prob unavailable "
            f"(probit={computed_err!r}, pdf=None)"
        )

    if pdf and computed:
        chosen = {**computed, **{k: v for k, v in pdf.items() if v is not None}}
        chosen["probit_crosscheck_pct"] = computed["value_pct"]
        if abs(pdf["value_pct"] - computed["value_pct"]) < 0.5:
            chosen["fetch_method"] = "nyfed_pdf+probit_check"
        else:
            chosen["fetch_method"] = "nyfed_pdf"
            chosen["note_divergence"] = True
    elif pdf:
        chosen = dict(pdf)
        chosen.setdefault("fetch_method", "nyfed_pdf")
    else:
        chosen = dict(computed)

    value_pct = clamp_recession_pct(float(chosen["value_pct"]), "ny_fed")
    date = str(chosen.get("date") or "")
    if not re.match(r"^\d{4}-\d{2}$", date) and not DATE_RE.match(date):
        raise RuntimeError(f"ny_fed_prob: bad date {date!r}")

    out = {
        "id": "ny_fed_yield_curve",
        "primary": True,
        "value_pct": round(value_pct, 2),
        "value_pct_raw": float(value_pct),
        "date": date[:7] if len(date) >= 7 else date,
        "horizon_label": chosen.get("horizon_label"),
        "model_name": "NY Fed — Probability of US Recession Predicted by Treasury Spread (10y−3m)",
        "model_name_zh": "紐約聯儲 · 國債期限利差衰退概率（10年−3個月，12個月前瞻）",
        "source_url": NYFED_PAGE,
        "chart_pdf_url": NYFED_PDF,
        "fred_related_url": "https://fred.stlouisfed.org/series/T10Y3M",
        "updated_on_source": chosen.get("updated_on_source"),
        "spread_pp": chosen.get("spread_pp"),
        "fetch_method": chosen.get("fetch_method"),
        "parameters": {"alpha": NYFED_ALPHA, "beta": NYFED_BETA, "sample": "1959-01 to 2009-12"},
        "note_zh": "公開模型（NY Fed / Estrella–Mishkin 類期限利差 probit），非本站預測、非投資建議。官方 CSV/XLS 目前未提供；數值來自官方 PDF 圖表及／或以 FRED T10Y3M 月均套用其公開參數。",
        "note_en": "Published NY Fed model, not this site's forecast. Not investment advice.",
    }
    if chosen.get("probit_crosscheck_pct") is not None:
        out["probit_crosscheck_pct"] = chosen["probit_crosscheck_pct"]
    return out


def fetch_chauvet_piger(api_key: str | None) -> dict[str, Any]:
    """Secondary: FRED RECPROUSM156N — smoothed *current* recession probability (Chauvet–Piger)."""
    start = (datetime.now(timezone.utc) - timedelta(days=800)).strftime("%Y-%m-%d")
    points = None
    method = "fred_csv_or_api"
    errors: list[str] = []
    if api_key:
        try:
            points = fetch_api_series("RECPROUSM156N", api_key, start=start)
            method = "fred_api"
        except Exception as e:
            errors.append(f"api:{e}")
    if points is None:
        try:
            points = fetch_csv_series("RECPROUSM156N", start=start)
            method = "fred_csv"
        except Exception as e:
            errors.append(f"csv:{e}")
    if points is None:
        local = DATA_DIR / "RECPROUSM156N.csv"
        if local.exists():
            pts = _load_csv_pairs(local)
            points = _validate_points("RECPROUSM156N", pts)
            method = "local_fred_cache"
    if not points:
        raise RuntimeError("RECPROUSM156N unavailable: " + "; ".join(errors))
    d, v = points[-1]
    value_pct = clamp_recession_pct(float(v), "chauvet_piger")
    if not DATE_RE.match(d):
        raise RuntimeError(f"RECPROUSM156N: bad date {d!r}")
    return {
        "id": "chauvet_piger_smoothed",
        "primary": False,
        "value_pct": round(value_pct, 2),
        "value_pct_raw": float(value_pct),
        "date": d[:7],
        "observation_date": d,
        "model_name": "Smoothed U.S. Recession Probabilities (Chauvet–Piger)",
        "model_name_zh": "平滑美國衰退概率（Chauvet–Piger，當前狀態）",
        "source_url": "https://fred.stlouisfed.org/series/RECPROUSM156N",
        "fred_id": "RECPROUSM156N",
        "note_zh": "公開模型：估計「而家是否處於衰退」嘅平滑概率，唔係 12 個月前瞻。非本站預測、非投資建議。",
        "note_en": "Published nowcast-style smoothed probability (not 12m-ahead). Not this site's forecast.",
        "fetch_method": method,
    }



# Pre-crash / pre-recession anchors for public-model comparison (not invented dates).
# Values = nearest month on/before target; NY Fed = published α/β × T10Y3M monthly avg.
CRASH_PROB_ANCHORS: list[dict[str, Any]] = [
    {
        "episode_id": "1990_recession",
        "label_zh": "1990 衰退前（~6個月）",
        "label_en": "1990 recession −6m",
        "role": "before_nber_~6m",
        "target_ym": "1990-01",
        "context_zh": "NBER 1990-07→1991-03",
        "show_in_main_table": True,
    },
    {
        "episode_id": "1990_recession",
        "label_zh": "1990 NBER 開始月",
        "label_en": "1990 NBER start",
        "role": "near_nber_start",
        "target_ym": "1990-07",
        "context_zh": "NBER 開始",
        "show_in_main_table": True,
    },
    {
        "episode_id": "2000_02_dotcom",
        "label_zh": "科網 · 股峰前~6個月",
        "label_en": "Dot-com −6m before equity peak",
        "role": "before_equity_peak_~6m",
        "target_ym": "1999-09",
        "context_zh": "標普高峰約 2000-03；NBER 2001-03→2001-11",
        "show_in_main_table": True,
    },
    {
        "episode_id": "2000_02_dotcom",
        "label_zh": "科網 · NBER 前~6個月",
        "label_en": "Dot-com −6m before NBER",
        "role": "before_nber_~6m",
        "target_ym": "2000-09",
        "context_zh": "NBER 2001-03 前約半年",
        "show_in_main_table": True,
    },
    {
        "episode_id": "2007_09_gfc",
        "label_zh": "金融海嘯 · 股峰前~6個月",
        "label_en": "GFC −6m before equity peak",
        "role": "before_equity_peak_~6m",
        "target_ym": "2007-04",
        "context_zh": "標普高峰約 2007-10；NBER 2007-12→2009-06",
        "show_in_main_table": True,
    },
    {
        "episode_id": "2007_09_gfc",
        "label_zh": "金融海嘯 · 股峰附近",
        "label_en": "GFC near equity peak",
        "role": "near_equity_peak",
        "target_ym": "2007-10",
        "context_zh": "股峰附近、NBER 前約 2 個月",
        "show_in_main_table": True,
    },
    {
        "episode_id": "2020_covid",
        "label_zh": "COVID · 股峰前~6個月",
        "label_en": "COVID −6m before equity peak",
        "role": "before_equity_peak_~6m",
        "target_ym": "2019-08",
        "context_zh": "標普高峰約 2020-02-19；NBER 2020-02→2020-04",
        "show_in_main_table": True,
    },
    {
        "episode_id": "1987_black_monday",
        "label_zh": "1987 黑色星期一前~6個月（事後套用）",
        "label_en": "1987 Black Monday −6m (retrospective)",
        "role": "before_crash_~6m",
        "target_ym": "1987-04",
        "context_zh": "股災 1987-10-19；非 NBER 衰退。當時未有而家呢份 NY Fed 公開圖表；用同一套公開 α/β 事後套用（參數樣本 1959–2009 含 1987）。",
        "retrospective_nyfed": True,
        "show_in_main_table": True,
    },
    # Chauvet–Piger rises into recessions (nowcast) — secondary rows
    {
        "episode_id": "2000_02_dotcom",
        "label_zh": "科網 · 衰退早期（CP）",
        "label_en": "Dot-com early recession (CP)",
        "role": "early_recession_cp",
        "target_ym": "2001-06",
        "context_zh": "Chauvet–Piger 進入衰退後上升（而家狀態，非 12m 前瞻）",
        "show_in_main_table": False,
        "cp_rising_row": True,
    },
    {
        "episode_id": "2007_09_gfc",
        "label_zh": "金融海嘯 · 衰退早期（CP）",
        "label_en": "GFC early recession (CP)",
        "role": "early_recession_cp",
        "target_ym": "2008-06",
        "context_zh": "Chauvet–Piger 進入衰退後上升",
        "show_in_main_table": False,
        "cp_rising_row": True,
    },
    {
        "episode_id": "2020_covid",
        "label_zh": "COVID · 衰退月（CP）",
        "label_en": "COVID recession month (CP)",
        "role": "early_recession_cp",
        "target_ym": "2020-03",
        "context_zh": "Chauvet–Piger 急升至極高（而家狀態）",
        "show_in_main_table": False,
        "cp_rising_row": True,
    },
]


def _monthly_averages(points: list[tuple[str, float]]) -> dict[str, float]:
    from collections import defaultdict

    by_m: dict[str, list[float]] = defaultdict(list)
    for d, v in points:
        by_m[d[:7]].append(v)
    return {ym: sum(vs) / len(vs) for ym, vs in by_m.items()}


def _nearest_ym(series: dict[str, float], target_ym: str) -> tuple[str, float] | None:
    keys = [k for k in series if k <= target_ym]
    if not keys:
        return None
    k = keys[-1]
    return k, series[k]


def _load_csv_pairs(path: Path, date_col_hint: str | None = None) -> list[tuple[str, float]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"{path}: empty")
    header = [h.strip() for h in lines[0].split(",")]
    # pick date col and value col
    date_i, val_i = 0, 1
    if date_col_hint and date_col_hint in header:
        date_i = header.index(date_col_hint)
    for i, h in enumerate(header):
        hu = h.upper()
        if hu in ("OBSERVATION_DATE", "DATE") or h.lower() == "date":
            date_i = i
        elif hu not in ("OBSERVATION_DATE", "DATE") and h.lower() != "date" and i != date_i:
            val_i = i
    # if 2 cols, value is the other
    if len(header) == 2:
        val_i = 1 - date_i
    out: list[tuple[str, float]] = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) <= max(date_i, val_i):
            continue
        d, v = parts[date_i].strip(), parts[val_i].strip()
        if not v or v == ".":
            continue
        try:
            out.append((d, float(v)))
        except ValueError:
            continue
    if not out:
        raise RuntimeError(f"{path}: no numeric rows")
    return out


def _curl_fred_csv(series_id: str, start: str | None = None, end: str | None = None, timeout: int = 45) -> list[tuple[str, float]]:
    """FRED graph CSV via curl --http1.1 (more reliable than urllib on some hosts)."""
    import subprocess

    params = {"id": series_id}
    if start:
        params["cosd"] = start
    if end:
        params["coed"] = end
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urllib.parse.urlencode(params)
    r = subprocess.run(
        ["curl", "--http1.1", "-sL", "--max-time", str(timeout), "-A", "us-leading-indicators/1.0", url],
        capture_output=True,
        timeout=timeout + 5,
    )
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"curl {series_id} failed rc={r.returncode}")
    text = r.stdout.decode("utf-8", errors="replace")
    return _parse_fred_csv_text(series_id, text)


def _load_t10y3m_history(api_key: str | None) -> tuple[dict[str, float], str]:
    """Monthly averages of T10Y3M for NY Fed reconstruction. Returns (ym→spread, source_tag)."""
    errors: list[str] = []
    if api_key:
        try:
            pts = fetch_api_series("T10Y3M", api_key, start="1982-01-01")
            return _monthly_averages(pts), "fred_api"
        except Exception as e:
            errors.append(f"api:{e}")
    try:
        raw, _ctype = _http_get("https://eco3min.fr/dataset/yield-curve-10y-3m.csv", timeout=45)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DATA_DIR / "T10Y3M_mirror.csv"
        tmp.write_bytes(raw)
        pts = _load_csv_pairs(tmp)
        return _monthly_averages(pts), "eco3min_fred_mirror"
    except Exception as e:
        errors.append(f"eco:{e}")
    local = DATA_DIR / "T10Y3M_mirror.csv"
    if local.exists():
        pts = _load_csv_pairs(local)
        return _monthly_averages(pts), "local_mirror_cache"
    try:
        pts = _curl_fred_csv("T10Y3M", start="1982-01-01", timeout=25)
        return _monthly_averages(pts), "fred_csv_curl"
    except Exception as e:
        errors.append(f"curl:{e}")
    raise RuntimeError("T10Y3M history unavailable: " + "; ".join(errors))



def _load_recpro_history(api_key: str | None) -> tuple[dict[str, float], str]:
    """RECPROUSM156N monthly map."""
    errors: list[str] = []
    if api_key:
        try:
            pts = fetch_api_series("RECPROUSM156N", api_key, start="1967-01-01")
            return {d[:7]: v for d, v in pts}, "fred_api"
        except Exception as e:
            errors.append(f"api:{e}")
    local = DATA_DIR / "RECPROUSM156N.csv"
    if local.exists():
        pts = _load_csv_pairs(local)
        series = {d[:7]: v for d, v in pts}
        try:
            tip = _curl_fred_csv("RECPROUSM156N", start="2024-01-01", timeout=25)
            for d, v in tip:
                series[d[:7]] = v
            lines = ["observation_date,RECPROUSM156N"] + [
                f"{ym}-01,{series[ym]}" for ym in sorted(series)
            ]
            local.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return series, "local_fred_cache+tip_curl"
        except Exception as e:
            errors.append(f"curl_tip:{e}")
            return series, "local_fred_cache"
    chunks = [
        ("1967-01-01", "1989-12-31"),
        ("1990-01-01", "2009-12-31"),
        ("2010-01-01", "2026-12-31"),
    ]
    merged: list[tuple[str, float]] = []
    try:
        for a, b in chunks:
            merged.extend(_curl_fred_csv("RECPROUSM156N", start=a, end=b, timeout=40))
        if merged:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            lines = ["observation_date,RECPROUSM156N"] + [f"{d},{v}" for d, v in merged]
            (DATA_DIR / "RECPROUSM156N.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
            return {d[:7]: v for d, v in merged}, "fred_csv_curl_chunked"
    except Exception as e:
        errors.append(f"curl:{e}")
    raise RuntimeError("RECPROUSM156N history unavailable: " + "; ".join(errors))



def build_crash_comparisons(
    api_key: str | None,
    ny_live: dict[str, Any] | None,
    cp_live: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Dated 而家 vs 大股災／衰退前 comparison for NY Fed (reconstructed) + Chauvet–Piger.
    Site rule risk is intentionally omitted from historical crash % (not calibrated).
    """
    t10, t10_src = _load_t10y3m_history(api_key)
    rec, rec_src = _load_recpro_history(api_key)

    rows: list[dict[str, Any]] = []

    # Current row — prefer live published figures
    now_ny = None
    if ny_live and ny_live.get("value_pct") is not None:
        now_ny = {
            "available": True,
            "value_pct": round(float(ny_live["value_pct"]), 2),
            "value_pct_raw": float(ny_live.get("value_pct_raw", ny_live["value_pct"])),
            "obs_ym": ny_live.get("date"),
            "spread_pp": ny_live.get("spread_pp"),
            "method": ny_live.get("fetch_method") or "live",
            "source_note": "Official NY Fed chart / same published α/β",
        }
    now_cp = None
    if cp_live and cp_live.get("value_pct") is not None:
        now_cp = {
            "available": True,
            "value_pct": round(float(cp_live["value_pct"]), 2),
            "value_pct_raw": float(cp_live.get("value_pct_raw", cp_live["value_pct"])),
            "obs_ym": cp_live.get("date"),
            "method": cp_live.get("fetch_method") or "live",
            "source_note": "FRED RECPROUSM156N",
        }
    rows.append(
        {
            "id": "now",
            "episode_id": "now",
            "label_zh": "而家",
            "label_en": "Now",
            "role": "current",
            "target_ym": (ny_live or {}).get("date") or (cp_live or {}).get("date"),
            "context_zh": "最新公開模型讀數",
            "show_in_main_table": True,
            "ny_fed": now_ny or {"available": False, "reason": "live_ny_unavailable"},
            "chauvet_piger": now_cp or {"available": False, "reason": "live_cp_unavailable"},
        }
    )

    for a in CRASH_PROB_ANCHORS:
        ym = a["target_ym"]
        ny_hit = _nearest_ym(t10, ym)
        cp_hit = _nearest_ym(rec, ym)
        ny_obj: dict[str, Any]
        if ny_hit:
            obs_ym, spread = ny_hit
            z = NYFED_ALPHA + NYFED_BETA * spread
            pct = _norm_cdf(z) * 100.0
            ny_obj = {
                "available": True,
                "value_pct": round(pct, 2),
                "value_pct_raw": round(pct, 4),
                "obs_ym": obs_ym,
                "target_ym": ym,
                "spread_pp": round(spread, 5),
                "method": "nyfed_probit_t10y3m_monthly_avg",
                "parameters": {"alpha": NYFED_ALPHA, "beta": NYFED_BETA, "sample": "1959-01 to 2009-12"},
                "retrospective": bool(a.get("retrospective_nyfed")),
                "source_note": "Published NY Fed α/β × FRED T10Y3M monthly average (not invented)",
            }
        else:
            ny_obj = {"available": False, "reason": "no_t10y3m", "target_ym": ym}

        cp_obj: dict[str, Any]
        if cp_hit:
            obs_ym, val = cp_hit
            cp_obj = {
                "available": True,
                "value_pct": round(float(val), 2),
                "value_pct_raw": float(val),
                "obs_ym": obs_ym,
                "target_ym": ym,
                "method": "fred_RECPROUSM156N",
                "source_note": "FRED RECPROUSM156N (smoothed; revises)",
            }
        else:
            cp_obj = {"available": False, "reason": "no_recpro", "target_ym": ym}

        rows.append(
            {
                "id": f"{a['episode_id']}__{a['role']}",
                "episode_id": a["episode_id"],
                "label_zh": a["label_zh"],
                "label_en": a["label_en"],
                "role": a["role"],
                "target_ym": ym,
                "context_zh": a.get("context_zh"),
                "show_in_main_table": bool(a.get("show_in_main_table", True)),
                "cp_rising_row": bool(a.get("cp_rising_row")),
                "ny_fed": ny_obj,
                "chauvet_piger": cp_obj,
            }
        )

    return {
        "title_zh": "而家 vs 大股災／衰退前",
        "title_en": "Now vs levels before major crashes / recessions",
        "methodology_zh": (
            "NY Fed：同一套公開 probit（α=−0.5333、β=−0.6330，樣本 1959-01–2009-12）"
            "套落 FRED T10Y3M 月均，計 12 個月前瞻衰退概率；「而家」優先用官方 PDF 圖表。"
            "Chauvet–Piger：FRED RECPROUSM156N 平滑「而家是否衰退」概率（會修訂）。"
            "錨點取目標月或之前最近一個月。數值唔會虛構。"
            "本站規則評估% 並非歷史校準／回測股災概率，故唔放入呢張對照表。"
        ),
        "methodology_en": (
            "NY Fed: published α/β on FRED T10Y3M monthly averages (12m-ahead); "
            "Now prefers official PDF. Chauvet–Piger: FRED RECPROUSM156N (nowcast; revises). "
            "Nearest month on/before each anchor. No invented numbers. "
            "Site rule risk % is not historically calibrated — omitted from this table."
        ),
        "sources": {
            "ny_fed_page": NYFED_PAGE,
            "ny_fed_pdf": NYFED_PDF,
            "t10y3m": "https://fred.stlouisfed.org/series/T10Y3M",
            "recpro": "https://fred.stlouisfed.org/series/RECPROUSM156N",
            "nber": "https://www.nber.org/research/data/us-business-cycle-expansions-and-contractions",
        },
        "fetch_sources": {"t10y3m": t10_src, "recpro": rec_src},
        "site_risk_note_zh": (
            "本站規則評估係領先指標投票映射，唔係概率校準、未做股災回測，"
            "所以對照表只放 NY Fed / Chauvet–Piger 公開模型。"
        ),
        "site_risk_note_en": (
            "Site rule risk is a transparent vote mapping — not a calibrated probability "
            "and not backtested to crashes. Crash history table covers published models only."
        ),
        "rows": rows,
        "generated_with": {
            "nyfed_alpha": NYFED_ALPHA,
            "nyfed_beta": NYFED_BETA,
        },
    }


def compute_site_risk(badge: dict[str, Any]) -> dict[str, Any]:
    """
    Map transparent vote score → 0–100 衰退／急挫風險%.

    Equal-weight mapping of each vote:
      +1 → 0%,  0 → 50%,  −1 → 100%
    Equivalent closed form:
      risk = 100 * (votes_cast − score) / (2 * votes_cast)
    Then clamp to [cap_min, cap_max] so we never imply false certainty of 0% or 100%.
    """
    votes = badge.get("votes") or []
    n = int(badge.get("votes_cast") or len(votes) or 0)
    score = int(badge.get("score") or 0)
    cap_min, cap_max = 5, 95
    components = []
    for v in votes:
        vote = int(v.get("vote") or 0)
        if vote > 0:
            r = 0.0
        elif vote < 0:
            r = 100.0
        else:
            r = 50.0
        components.append({"name": v.get("name"), "vote": vote, "risk_contrib": r, "detail": v.get("detail")})
    if n <= 0:
        raw = 50.0
        formula_used = "no_votes→50"
    else:
        raw = 100.0 * (n - score) / (2.0 * n)
        formula_used = "100*(n-score)/(2*n)"
    clamped = max(cap_min, min(cap_max, raw))
    return {
        "id": "site_rule_risk",
        "label_zh": "本站規則評估 · 衰退／急挫風險",
        "label_en": "Site rule-based recession/crash risk",
        "value_pct": round(clamped, 1),
        "value_pct_raw": round(raw, 4),
        "score": score,
        "votes_cast": n,
        "status": badge.get("status"),
        "status_en": badge.get("status_en"),
        "tone": badge.get("tone"),
        "cap_min": cap_min,
        "cap_max": cap_max,
        "formula_zh": (
            "各訊號票等權：+1→0%、0→50%、−1→100%，再平均；"
            f"等價式 risk = 100×(票數−得分)/(2×票數)，再夾喺 {cap_min}–{cap_max}。"
            "透明規則，非 ML、非 NY Fed 模型。"
        ),
        "formula_en": (
            "Equal-weight: +1→0%, 0→50%, −1→100%, then average; "
            f"equiv. 100*(n-score)/(2*n), clamped to [{cap_min},{cap_max}]. Not ML."
        ),
        "formula_used": formula_used,
        "components": components,
        "note_zh": "由本頁同一套領先指標投票映射而成，唔係概率校準、未做歷史股災回測，唔係投資建議。",
        "note_en": "Mapped from the same on-page votes — not a calibrated probability. Not investment advice.",
    }


def compute_badge(fred: dict[str, Any], manual: dict[str, Any]) -> dict[str, Any]:
    """
    Transparent rule-based status. Each signal votes +1 (偏牛), -1 (偏熊), 0 (中性).
    Documented in README.
    """
    votes: list[dict[str, Any]] = []

    def add(name: str, vote: int, detail: str) -> None:
        votes.append({"name": name, "vote": vote, "detail": detail})

    # 1) 2s10s sign
    t = fred.get("T10Y2Y")
    if t:
        v = t["value"]
        if v > 0.1:
            add("2s10s", 1, f"+{v:.2f} pp 正斜率")
        elif v < -0.05:
            add("2s10s", -1, f"{v:.2f} pp 倒掛")
        else:
            add("2s10s", 0, f"{v:.2f} pp 接近平坦")

    # 2) ISM PMI vs 50
    ism = (manual.get("indicators") or {}).get("ISM_PMI") or {}
    if ism.get("value") is not None:
        pv = float(ism["value"])
        if pv >= 52:
            add("ISM PMI", 1, f"{pv} ≥ 52 擴張")
        elif pv < 48:
            add("ISM PMI", -1, f"{pv} < 48 收縮")
        else:
            add("ISM PMI", 0, f"{pv} 近臨界")

    # 3) Claims trend: latest vs ~4 weeks earlier 4-wk avg or ICSA
    icsa = fred.get("ICSA")
    ic4 = fred.get("IC4WSA")
    claims_src = ic4 or icsa
    if claims_src and len(claims_src.get("history") or []) >= 5:
        hist = [(h["d"], h["v"]) for h in claims_src["history"]]
        # history already sparklined; use value vs value from start of spark if short
        # Better: re-fetch trend from stored meta
        latest = claims_src["value"]
        # Prefer comparing IC4WSA level: >250k soft, rising >5% bearish
        prior = hist[max(0, len(hist) - 5)][1]
        pct = (latest - prior) / prior if prior else 0
        if latest > 280000 or pct > 0.08:
            add("失業救濟", -1, f"{int(latest):,}（相對近期升幅/高水平）")
        elif latest < 230000 and pct < 0.03:
            add("失業救濟", 1, f"{int(latest):,} 低位穩定")
        else:
            add("失業救濟", 0, f"{int(latest):,} 中性")

    # 4) HY OAS direction: widen = bearish. Use value thresholds + recent change from history
    hy = fred.get("BAMLH0A0HYM2")
    if hy:
        hv = hy["value"]
        hist = hy.get("history") or []
        prior = hist[max(0, len(hist) - 22)]["v"] if len(hist) >= 2 else hv
        widen = hv - prior
        if hv >= 5.0 or widen >= 0.5:
            add("HY OAS", -1, f"{hv:.2f}%（走闊）")
        elif hv <= 3.5 and widen <= 0.15:
            add("HY OAS", 1, f"{hv:.2f}% 偏緊")
        else:
            add("HY OAS", 0, f"{hv:.2f}% 中性")

    # 5) SPX vs recent (~60 trading days ≈ last ~1/3 of spark)
    spx = fred.get("SP500")
    if spx and spx.get("history"):
        hist = spx["history"]
        latest = spx["value"]
        prior = hist[max(0, len(hist) // 3)]["v"]
        ret = (latest - prior) / prior if prior else 0
        if ret > 0.03:
            add("標普500", 1, f"相對近期 +{ret*100:.1f}%")
        elif ret < -0.05:
            add("標普500", -1, f"相對近期 {ret*100:.1f}%")
        else:
            add("標普500", 0, f"相對近期 {ret*100:+.1f}%")

    score = sum(v["vote"] for v in votes)
    n = len(votes) or 1
    if score >= 2:
        status = "偏牛"
        status_en = "Lean bullish"
        tone = "bull"
    elif score <= -2:
        status = "偏熊"
        status_en = "Lean bearish"
        tone = "bear"
    else:
        status = "過渡"
        status_en = "Transitional"
        tone = "neutral"

    return {
        "status": status,
        "status_en": status_en,
        "tone": tone,
        "score": score,
        "votes_cast": n,
        "votes": votes,
        "rule_summary_zh": "票數加總：≥+2 偏牛；≤−2 偏熊；其餘過渡。訊號：2s10s、ISM PMI、失業救濟、HY OAS、標普500。",
    }


def _series_value_ok(series_id: str, date: str, value: float) -> bool:
    if not DATE_RE.match(str(date)[:10] if len(str(date)) >= 10 else str(date)):
        # allow YYYY-MM-DD only for fred series
        if not DATE_RE.match(str(date)):
            return False
    lo_hi = VALUE_RANGES.get(series_id)
    if lo_hi is None:
        return True
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return lo_hi[0] <= v <= lo_hi[1]


def load_previous_payload() -> dict[str, Any] | None:
    """Load prior docs/data.json if present and not obviously corrupt."""
    if not OUT_PATH.exists():
        return None
    try:
        prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(prev, dict):
        return None
    fred = prev.get("fred") or {}
    # If majority of series look like HTML artifacts, treat as corrupt and ignore.
    bad = 0
    total = 0
    for sid, row in fred.items():
        if not isinstance(row, dict):
            continue
        total += 1
        if not _series_value_ok(sid, str(row.get("date") or ""), row.get("value")):
            bad += 1
    if total and bad >= max(1, total // 2):
        print(f"  Ignoring corrupt previous data.json ({bad}/{total} series failed sanity)", file=sys.stderr)
        return None
    return prev


def fetch_h15_treasury_spreads() -> dict[str, list[tuple[str, float]]]:
    """
    Fed H.15 constant-maturity yields → T10Y2Y / T10Y3M when FRED is down.
    Columns: 1m,3m,6m,1y,2y,3y,5y,7y,10y,20y,30y after Time Period.
    """
    import csv
    import io

    url = (
        "https://www.federalreserve.gov/datadownload/Output.aspx?"
        "rel=H15&series=bf17364827e38702b42a58cf8eaa3f78&lastobs=180"
        "&from=&to=&filetype=csv&label=include&layout=seriescolumn&type=package"
    )
    import subprocess

    text = ""
    try:
        raw, _ctype = _http_get(url, timeout=30)
        text = raw.decode("utf-8", errors="replace")
    except Exception as e_urllib:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", "45", "-A", USER_AGENT, url],
            capture_output=True,
            timeout=50,
        )
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError(f"H.15 urllib failed ({e_urllib}); curl rc={r.returncode}") from e_urllib
        text = r.stdout.decode("utf-8", errors="replace")
    if _looks_like_html(text):
        # retry once via curl even if urllib returned an HTML interstitial
        r = subprocess.run(
            ["curl", "-sL", "--max-time", "45", "-A", USER_AGENT, url],
            capture_output=True,
            timeout=50,
        )
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError("H.15 returned HTML")
        text = r.stdout.decode("utf-8", errors="replace")
        if _looks_like_html(text):
            raise RuntimeError("H.15 returned HTML")
    rows = list(csv.reader(io.StringIO(text)))
    data_start = None
    for i, r in enumerate(rows):
        if r and r[0].strip() == "Time Period":
            data_start = i + 1
            break
    if data_start is None:
        raise RuntimeError("H.15: Time Period header not found")
    t10y2y: list[tuple[str, float]] = []
    t10y3m: list[tuple[str, float]] = []
    t10yff_approx: list[tuple[str, float]] = []  # unused; DFF separate
    for r in rows[data_start:]:
        if len(r) < 10:
            continue
        d = r[0].strip()
        if not DATE_RE.match(d):
            continue
        def _f(cell: str) -> float | None:
            cell = (cell or "").strip()
            if not cell or cell.upper() == "ND":
                return None
            try:
                return float(cell)
            except ValueError:
                return None
        y2, y3m, y10 = _f(r[5]), _f(r[2]), _f(r[9])
        if y10 is not None and y2 is not None:
            t10y2y.append((d, round(y10 - y2, 4)))
        if y10 is not None and y3m is not None:
            t10y3m.append((d, round(y10 - y3m, 4)))
    if not t10y2y:
        raise RuntimeError("H.15: no T10Y2Y points")
    return {"T10Y2Y": t10y2y, "T10Y3M": t10y3m}


def fetch_one(series_id: str, meta: dict[str, Any], api_key: str | None) -> dict[str, Any]:
    start = start_for(meta["lookback_days"])
    points: list[tuple[str, float]] | None = None
    source: str = "unknown"
    errors: list[str] = []

    if api_key:
        try:
            points = fetch_api_series(series_id, api_key, start=start)
            source = "fred_api"
        except Exception as e:
            errors.append(f"api:{e}")
            print(f"  API failed for {series_id} ({e}); trying CSV", file=sys.stderr)

    if points is None:
        try:
            points = fetch_csv_series(series_id, start=start)
            source = "fred_csv" if not api_key else "fred_csv_fallback"
        except Exception as e:
            errors.append(f"csv:{e}")

    # Fed H.15 fallback for 2s10s when FRED graph/API are in maintenance
    if points is None and series_id == "T10Y2Y":
        try:
            points = fetch_h15_treasury_spreads()["T10Y2Y"]
            source = "fed_h15_derived"
        except Exception as e:
            errors.append(f"h15:{e}")

    if not points:
        raise RuntimeError(f"{series_id}: fetch failed — " + " | ".join(errors))

    points = _validate_points(series_id, points)

    # Keep only lookback window
    cutoff = (datetime.now(timezone.utc) - timedelta(days=meta["lookback_days"])).strftime("%Y-%m-%d")
    points = [(d, v) for d, v in points if d >= cutoff] or points[-60:]

    latest_d, latest_v = points[-1]
    if not _series_value_ok(series_id, latest_d, latest_v):
        raise RuntimeError(f"{series_id}: latest observation failed sanity ({latest_d}, {latest_v})")
    return {
        "id": series_id,
        "label_zh": meta["label_zh"],
        "label_en": meta["label_en"],
        "unit": meta["unit"],
        "value": latest_v,
        "value_fmt": format_value(series_id, latest_v),
        "date": latest_d,
        "trend": trend_label(points, series_id),
        "history": sparkline(points),
        "source": source,
        "fred_url": f"https://fred.stlouisfed.org/series/{series_id}",
    }


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    api_key = (os.environ.get("FRED_API_KEY") or "").strip() or None
    mode = "api+csv_fallback" if api_key else "csv_keyless"
    print(f"Fetch mode: {mode}")

    previous = load_previous_payload()
    prev_fred = (previous or {}).get("fred") or {}

    fred: dict[str, Any] = {}
    errors: list[str] = []
    kept_previous: list[str] = []
    for sid, meta in SERIES.items():
        print(f"Fetching {sid}...")
        try:
            fred[sid] = fetch_one(sid, meta, api_key)
            print(f"  → {fred[sid]['value_fmt']} @ {fred[sid]['date']} [{fred[sid]['source']}]")
        except Exception as e:
            msg = f"{sid}: {e}"
            print(f"  ERROR {msg}", file=sys.stderr)
            errors.append(msg)
            prev_row = prev_fred.get(sid)
            if (
                isinstance(prev_row, dict)
                and _series_value_ok(sid, str(prev_row.get("date") or ""), prev_row.get("value"))
            ):
                kept = dict(prev_row)
                kept["source"] = f"previous_good:{(prev_row.get('source') or 'unknown')}"
                kept["stale"] = True
                fred[sid] = kept
                kept_previous.append(sid)
                print(
                    f"  → kept previous good {kept.get('value_fmt')} @ {kept.get('date')} [previous_good]",
                    file=sys.stderr,
                )

    manual = load_manual()
    badge = compute_badge(fred, manual)
    site_risk = compute_site_risk(badge)
    now_hkt = datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT")

    public_models: list[dict[str, Any]] = []
    public_probs: dict[str, Any] = {
        "generated_at": now_hkt,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disclaimer_zh": "公開模型數值，非本站預測、非投資建議。",
        "disclaimer_en": "Published model figures — not this site's forecast. Not investment advice.",
        "models": [],
        "site_risk": site_risk,
    }

    prev_models = ((previous or {}).get("public_probs") or {}).get("models") or []

    def _prev_model(mid: str) -> dict[str, Any] | None:
        for m in prev_models:
            if isinstance(m, dict) and m.get("id") == mid:
                try:
                    clamp_recession_pct(float(m.get("value_pct")), mid)
                except Exception:
                    return None
                return m
        return None

    print("Fetching NY Fed recession probability...")
    try:
        ny = fetch_ny_fed_prob(api_key)
        public_models.append(ny)
        print(f"  → NY Fed {ny['value_pct']}% as-of {ny['date']} [{ny['fetch_method']}]")
    except Exception as e:
        msg = f"ny_fed_prob: {e}"
        print(f"  ERROR {msg}", file=sys.stderr)
        errors.append(msg)
        prev_ny = _prev_model("ny_fed_yield_curve")
        if prev_ny:
            kept = dict(prev_ny)
            kept["fetch_method"] = f"previous_good:{(prev_ny.get('fetch_method') or 'unknown')}"
            kept["stale"] = True
            public_models.append(kept)
            print(f"  → kept previous NY Fed {kept.get('value_pct')}% @ {kept.get('date')}", file=sys.stderr)

    print("Fetching Chauvet–Piger (RECPROUSM156N)...")
    try:
        cp = fetch_chauvet_piger(api_key)
        public_models.append(cp)
        print(f"  → Chauvet–Piger {cp['value_pct']}% as-of {cp['date']}")
    except Exception as e:
        msg = f"RECPROUSM156N: {e}"
        print(f"  ERROR {msg}", file=sys.stderr)
        errors.append(msg)
        prev_cp = _prev_model("chauvet_piger_smoothed")
        if prev_cp:
            kept = dict(prev_cp)
            kept["fetch_method"] = f"previous_good:{(prev_cp.get('fetch_method') or 'unknown')}"
            kept["stale"] = True
            public_models.append(kept)
            print(f"  → kept previous Chauvet–Piger {kept.get('value_pct')}% @ {kept.get('date')}", file=sys.stderr)

    public_probs["models"] = public_models
    primary = next((m for m in public_models if m.get("primary")), public_models[0] if public_models else None)

    ny_live = next((m for m in public_models if m.get("id") == "ny_fed_yield_curve"), None)
    cp_live = next((m for m in public_models if m.get("id") == "chauvet_piger_smoothed"), None)
    crash_comp = None
    print("Building crash/recession probability comparisons...")
    try:
        crash_comp = build_crash_comparisons(api_key, ny_live, cp_live)
        # attach current site risk % into the honesty note
        crash_comp["site_risk_note_zh"] = (
            f"本站規則評估而家 {site_risk['value_pct']}% 係領先指標投票映射，"
            "唔係概率校準、未做股災回測，所以對照表只放 NY Fed / Chauvet–Piger 公開模型。"
        )
        crash_comp["site_risk_current_pct"] = site_risk["value_pct"]
        public_probs["crash_comparisons"] = crash_comp
        n_main = sum(1 for r in crash_comp["rows"] if r.get("show_in_main_table"))
        print(f"  → {n_main} main-table rows (t10={crash_comp['fetch_sources']['t10y3m']}, "
              f"recpro={crash_comp['fetch_sources']['recpro']})")
    except Exception as e:
        msg = f"crash_comparisons: {e}"
        print(f"  ERROR {msg}", file=sys.stderr)
        errors.append(msg)

    payload = {
        "generated_at": now_hkt,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fetch_mode": mode,
        "badge": badge,
        "site_risk": site_risk,
        "public_probs": {
            "primary": primary,
            "models": public_models,
            "crash_comparisons": crash_comp,
            "disclaimer_zh": public_probs["disclaimer_zh"],
            "disclaimer_en": public_probs["disclaimer_en"],
        },
        "fred": fred,
        "manual": manual,
        "errors": errors,
        "disclaimer_zh": "只供參考，非投資建議。公開衰退概率係官方／FRED 已發布模型，唔係本站預測。",
        "disclaimer_en": "For reference only. Not investment advice. Public recession probs are published models, not this site's forecast.",
    }

    # Final sanity gate — never persist HTML-parse artifacts (e.g. 400% probs).
    for sid, row in fred.items():
        if not _series_value_ok(sid, str(row.get("date") or ""), row.get("value")):
            raise RuntimeError(f"refusing to write corrupt series {sid}: {row.get('date')!r}={row.get('value')!r}")
    for m in public_models:
        clamp_recession_pct(float(m["value_pct"]), m.get("id") or "model")
        d = str(m.get("date") or "")
        if not (DATE_RE.match(d) or _re_ym.match(d)):
            raise RuntimeError(f"refusing to write model {m.get('id')} with bad date {d!r}")

    payload["kept_previous_series"] = kept_previous
    public_probs["models"] = public_models

    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PUBLIC_PROBS_PATH.write_text(json.dumps(public_probs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {OUT_PATH} + {PUBLIC_PROBS_PATH} "
        f"(badge={badge['status']}, site_risk={site_risk['value_pct']}%, "
        f"series={len(fred)}, public_models={len(public_models)}, "
        f"kept_previous={len(kept_previous)}, errors={len(errors)})"
    )
    # Success if we have usable series OR public models; partial FRED outage is OK.
    return 0 if (fred or public_models) else 1


if __name__ == "__main__":
    raise SystemExit(main())
