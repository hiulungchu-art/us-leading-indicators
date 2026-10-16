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


def _http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "us-leading-indicators/1.0 (GitHub Pages; educational)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_csv_series(series_id: str, start: str | None = None) -> list[tuple[str, float]]:
    """Keyless FRED graph CSV. Returns [(date_iso, value), ...] oldest→newest, drops blanks."""
    params = {"id": series_id}
    if start:
        params["cosd"] = start
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urllib.parse.urlencode(params)
    raw = _http_get(url).decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"{series_id}: empty CSV")
    out: list[tuple[str, float]] = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if not v or v.upper() == ".":
            continue
        try:
            out.append((d, float(v)))
        except ValueError:
            continue
    if not out:
        raise RuntimeError(f"{series_id}: no numeric observations")
    return out


def fetch_api_series(series_id: str, api_key: str, start: str | None = None) -> list[tuple[str, float]]:
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "asc",
    }
    if start:
        params["observation_start"] = start
    url = "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(params)
    data = json.loads(_http_get(url).decode("utf-8"))
    obs = data.get("observations") or []
    out: list[tuple[str, float]] = []
    for o in obs:
        v = (o.get("value") or "").strip()
        if not v or v == ".":
            continue
        try:
            out.append((o["date"], float(v)))
        except (ValueError, KeyError):
            continue
    if not out:
        raise RuntimeError(f"{series_id}: API returned no observations")
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
        raw = _http_get(NYFED_PDF, timeout=45)
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
    value_pct = round(float(prob_s), 4)
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
    if api_key:
        try:
            points = fetch_api_series("T10Y3M", api_key, start=start)
        except Exception:
            points = fetch_csv_series("T10Y3M", start=start)
    else:
        points = fetch_csv_series("T10Y3M", start=start)
    got = _monthly_avg_last_complete(points)
    if not got:
        raise RuntimeError("T10Y3M: no monthly average")
    ym, spread = got
    z = NYFED_ALPHA + NYFED_BETA * spread
    value_pct = round(_norm_cdf(z) * 100.0, 4)
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
        "fetch_method": "nyfed_probit_t10y3m",
        "fred_spread_id": "T10Y3M",
        "parameters": {"alpha": NYFED_ALPHA, "beta": NYFED_BETA},
    }


def fetch_ny_fed_prob(api_key: str | None) -> dict[str, Any]:
    """
    Primary public model: NY Fed 12-month-ahead recession probability from 10y−3m spread.
    Prefer official PDF print; fall back to same published formula on FRED T10Y3M.
    """
    computed = compute_nyfed_from_fred(api_key)
    pdf = parse_nyfed_pdf()
    chosen = computed
    if pdf and abs(pdf["value_pct"] - computed["value_pct"]) < 0.5:
        # Trust PDF when it matches the model within rounding/noise
        chosen = {**computed, **{k: v for k, v in pdf.items() if v is not None}}
        chosen["fetch_method"] = "nyfed_pdf+probit_check"
        chosen["probit_crosscheck_pct"] = computed["value_pct"]
    elif pdf:
        # PDF available but diverges — still prefer PDF printed official chart
        chosen = {**computed, **{k: v for k, v in pdf.items() if v is not None}}
        chosen["fetch_method"] = "nyfed_pdf"
        chosen["probit_crosscheck_pct"] = computed["value_pct"]
        chosen["note_divergence"] = True

    out = {
        "id": "ny_fed_yield_curve",
        "primary": True,
        "value_pct": round(float(chosen["value_pct"]), 2),
        "value_pct_raw": float(chosen["value_pct"]),
        "date": chosen["date"],
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
    if api_key:
        try:
            points = fetch_api_series("RECPROUSM156N", api_key, start=start)
        except Exception:
            points = fetch_csv_series("RECPROUSM156N", start=start)
    else:
        points = fetch_csv_series("RECPROUSM156N", start=start)
    d, v = points[-1]
    return {
        "id": "chauvet_piger_smoothed",
        "primary": False,
        "value_pct": round(float(v), 2),
        "value_pct_raw": float(v),
        "date": d[:7] if len(d) >= 7 else d,
        "observation_date": d,
        "model_name": "Smoothed U.S. Recession Probabilities (Chauvet–Piger)",
        "model_name_zh": "平滑美國衰退概率（Chauvet–Piger，當前狀態）",
        "source_url": "https://fred.stlouisfed.org/series/RECPROUSM156N",
        "fred_id": "RECPROUSM156N",
        "note_zh": "公開模型：估計「而家是否處於衰退」嘅平滑概率，唔係 12 個月前瞻。非本站預測、非投資建議。",
        "note_en": "Published nowcast-style smoothed probability (not 12m-ahead). Not this site's forecast.",
        "fetch_method": "fred_csv_or_api",
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
        "note_zh": "由本頁同一套領先指標投票映射而成，唔係概率校準、唔係投資建議。",
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


def fetch_one(series_id: str, meta: dict[str, Any], api_key: str | None) -> dict[str, Any]:
    start = start_for(meta["lookback_days"])
    points: list[tuple[str, float]]
    source: str
    if api_key:
        try:
            points = fetch_api_series(series_id, api_key, start=start)
            source = "fred_api"
        except Exception as e:
            print(f"  API failed for {series_id} ({e}); falling back to CSV", file=sys.stderr)
            points = fetch_csv_series(series_id, start=start)
            source = "fred_csv_fallback"
    else:
        points = fetch_csv_series(series_id, start=start)
        source = "fred_csv"

    # Keep only lookback window
    cutoff = (datetime.now(timezone.utc) - timedelta(days=meta["lookback_days"])).strftime("%Y-%m-%d")
    points = [(d, v) for d, v in points if d >= cutoff] or points[-60:]

    latest_d, latest_v = points[-1]
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

    fred: dict[str, Any] = {}
    errors: list[str] = []
    for sid, meta in SERIES.items():
        print(f"Fetching {sid}...")
        try:
            fred[sid] = fetch_one(sid, meta, api_key)
            print(f"  → {fred[sid]['value_fmt']} @ {fred[sid]['date']} [{fred[sid]['source']}]")
        except Exception as e:
            msg = f"{sid}: {e}"
            print(f"  ERROR {msg}", file=sys.stderr)
            errors.append(msg)

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

    print("Fetching NY Fed recession probability...")
    try:
        ny = fetch_ny_fed_prob(api_key)
        public_models.append(ny)
        print(f"  → NY Fed {ny['value_pct']}% as-of {ny['date']} [{ny['fetch_method']}]")
    except Exception as e:
        msg = f"ny_fed_prob: {e}"
        print(f"  ERROR {msg}", file=sys.stderr)
        errors.append(msg)

    print("Fetching Chauvet–Piger (RECPROUSM156N)...")
    try:
        cp = fetch_chauvet_piger(api_key)
        public_models.append(cp)
        print(f"  → Chauvet–Piger {cp['value_pct']}% as-of {cp['date']}")
    except Exception as e:
        msg = f"RECPROUSM156N: {e}"
        print(f"  ERROR {msg}", file=sys.stderr)
        errors.append(msg)

    public_probs["models"] = public_models
    primary = next((m for m in public_models if m.get("primary")), public_models[0] if public_models else None)

    payload = {
        "generated_at": now_hkt,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fetch_mode": mode,
        "badge": badge,
        "site_risk": site_risk,
        "public_probs": {
            "primary": primary,
            "models": public_models,
            "disclaimer_zh": public_probs["disclaimer_zh"],
            "disclaimer_en": public_probs["disclaimer_en"],
        },
        "fred": fred,
        "manual": manual,
        "errors": errors,
        "disclaimer_zh": "只供參考，非投資建議。公開衰退概率係官方／FRED 已發布模型，唔係本站預測。",
        "disclaimer_en": "For reference only. Not investment advice. Public recession probs are published models, not this site's forecast.",
    }

    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    PUBLIC_PROBS_PATH.write_text(json.dumps(public_probs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {OUT_PATH} + {PUBLIC_PROBS_PATH} "
        f"(badge={badge['status']}, site_risk={site_risk['value_pct']}%, "
        f"series={len(fred)}, public_models={len(public_models)}, errors={len(errors)})"
    )
    return 0 if fred else 1


if __name__ == "__main__":
    raise SystemExit(main())
