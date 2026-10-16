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
    now_hkt = datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT")

    payload = {
        "generated_at": now_hkt,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fetch_mode": mode,
        "badge": badge,
        "fred": fred,
        "manual": manual,
        "errors": errors,
        "disclaimer_zh": "只供參考，非投資建議。",
        "disclaimer_en": "For reference only. Not investment advice.",
    }

    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_PATH} (badge={badge['status']}, series={len(fred)}, errors={len(errors)})")
    return 0 if fred else 1


if __name__ == "__main__":
    raise SystemExit(main())
