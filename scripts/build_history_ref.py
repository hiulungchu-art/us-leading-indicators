#!/usr/bin/env python3
"""
Build docs/history_ref.json: long-run normals + crash/recession episode snapshots.

Data: FRED API when FRED_API_KEY is set; else keyless CSV
(https://fred.stlouisfed.org/graph/fredgraph.csv?id=SERIES).
Do not invent numbers — only observed FRED values (nearest observation on/before date).
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT = DOCS / "history_ref.json"

HKT = timezone(timedelta(hours=8))

# Series used for normals + episodes
SERIES_META: dict[str, dict[str, str]] = {
    "T10Y2Y": {"label_zh": "2s10s 孳息曲線", "label_en": "2s10s", "unit": "pp"},
    "T10YFF": {"label_zh": "10年−聯邦基金", "label_en": "10y−FF", "unit": "pp"},
    "ICSA": {"label_zh": "初領失業救濟", "label_en": "Initial claims", "unit": "claims"},
    "IC4WSA": {"label_zh": "初領4週平均", "label_en": "Claims 4w avg", "unit": "claims"},
    "BAMLH0A0HYM2": {"label_zh": "美高收益OAS", "label_en": "HY OAS", "unit": "%"},
    "BAA10Y": {"label_zh": "BAA−10年國債利差", "label_en": "Moody's BAA−10y", "unit": "pp"},
    "SP500": {"label_zh": "標普500", "label_en": "S&P 500", "unit": "index"},
    "UNRATE": {"label_zh": "失業率", "label_en": "Unemployment", "unit": "%"},
    "UMCSENT": {"label_zh": "密歇根消費者信心", "label_en": "U. Michigan sentiment", "unit": "index"},
    "PERMIT": {"label_zh": "建築許可", "label_en": "Building permits", "unit": "SAAR thou"},
    "INDPRO": {"label_zh": "工業生產指數", "label_en": "Industrial production", "unit": "index"},
}

# Crash / recession episodes (NBER + SPX drawdown anchors)
# Snapshot dates are chosen as calendar anchors; values = nearest obs on/before date.
EPISODES: list[dict[str, Any]] = [
    {
        "id": "1987_black_monday",
        "name_zh": "1987 黑色星期一",
        "name_en": "1987 Black Monday",
        "kind": "equity_crash",
        "nber_recession": None,
        "notes_zh": "股市急挫（非 NBER 衰退）。部分序列當時未存在或樣本極短。",
        "peak_stress": "1987-10-19",
        "before": "1987-04-19",   # ~6m before
        "trough": "1987-12-04",   # SPX local low area Dec 1987
        "after": "1988-10-19",    # ~12m after peak stress
    },
    {
        "id": "2000_02_dotcom",
        "name_zh": "2000–02 科網泡沫",
        "name_en": "2000–02 Dot-com",
        "kind": "equity_crash_and_recession",
        "nber_recession": {"start": "2001-03-01", "end": "2001-11-01"},
        "notes_zh": "股市高峰約 2000-03；NBER 衰退 2001-03 至 2001-11；標普大致低位約 2002-10。",
        "peak_stress": "2000-03-24",  # near equity peak / early unwind
        "before": "1999-09-24",
        "trough": "2002-10-09",       # SPX bear-market low area
        "after": "2003-10-09",
    },
    {
        "id": "2007_09_gfc",
        "name_zh": "2007–09 金融海嘯",
        "name_en": "2007–09 GFC",
        "kind": "equity_crash_and_recession",
        "nber_recession": {"start": "2007-12-01", "end": "2009-06-01"},
        "notes_zh": "NBER 衰退 2007-12 至 2009-06；標普低位約 2009-03。",
        "peak_stress": "2007-10-09",  # near SPX peak
        "before": "2007-04-09",
        "trough": "2009-03-09",
        "after": "2010-03-09",
    },
    {
        "id": "2020_covid",
        "name_zh": "2020 COVID 急挫",
        "name_en": "2020 COVID crash",
        "kind": "equity_crash_and_recession",
        "nber_recession": {"start": "2020-02-01", "end": "2020-04-01"},
        "notes_zh": "NBER 衰退 2020-02 至 2020-04；標普低位約 2020-03-23。",
        "peak_stress": "2020-02-19",
        "before": "2019-08-19",
        "trough": "2020-03-23",
        "after": "2021-03-23",
    },
]

EPISODE_SERIES = [
    "T10Y2Y",
    "T10YFF",
    "BAMLH0A0HYM2",
    "BAA10Y",
    "ICSA",
    "IC4WSA",
    "UNRATE",
    "UMCSENT",
    "INDPRO",
    "PERMIT",
    "SP500",
]

# FRED licensing truncations (document in JSON; do not invent older prints)
FRED_COVERAGE_NOTES = {
    "BAMLH0A0HYM2": "As of ~Apr 2026 FRED keeps ~3y of ICE BofA HY OAS; older history not on FRED.",
    "SP500": "FRED S&P 500 daily history limited to ~10y under S&P DJI agreement.",
}


def _http_get(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "us-leading-indicators/1.0 (GitHub Pages; educational)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_api(series_id: str, api_key: str) -> list[tuple[str, float]]:
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "asc",
    }
    url = "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(params)
    data = json.loads(_http_get(url).decode("utf-8"))
    if "error_message" in data:
        raise RuntimeError(f"{series_id}: {data['error_message']}")
    out: list[tuple[str, float]] = []
    for o in data.get("observations") or []:
        v = (o.get("value") or "").strip()
        if not v or v == ".":
            continue
        try:
            out.append((o["date"], float(v)))
        except (KeyError, ValueError):
            continue
    if not out:
        raise RuntimeError(f"{series_id}: no observations")
    return out


def fetch_csv(series_id: str) -> list[tuple[str, float]]:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urllib.parse.urlencode({"id": series_id})
    raw = _http_get(url, timeout=180).decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
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
        raise RuntimeError(f"{series_id}: empty CSV")
    return out


def fetch_series(series_id: str, api_key: str | None) -> tuple[list[tuple[str, float]], str]:
    if api_key:
        return fetch_api(series_id, api_key), "fred_api"
    return fetch_csv(series_id), "fred_csv_keyless"


def parse_d(s: str) -> date:
    return date.fromisoformat(s[:10])


def nearest_on_or_before(obs: list[tuple[str, float]], target: str, max_lag_days: int = 45) -> dict[str, Any] | None:
    """Return {date, value} for last obs on/before target, if within max_lag_days."""
    t = parse_d(target)
    best: tuple[str, float] | None = None
    for d, v in obs:
        dd = parse_d(d)
        if dd <= t:
            best = (d, v)
        else:
            break
    if best is None:
        return None
    lag = (t - parse_d(best[0])).days
    if lag > max_lag_days:
        return {"date": best[0], "value": best[1], "lag_days": lag, "stale": True}
    return {"date": best[0], "value": best[1], "lag_days": lag}


def fmt_value(series_id: str, v: float) -> str:
    unit = SERIES_META[series_id]["unit"]
    if unit == "claims":
        return f"{v:,.0f}"
    if unit == "SAAR thou":
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:,.1f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def percentile(sorted_vals: list[float], p: float) -> float:
    """Linear interpolation percentile, p in [0,100]."""
    if not sorted_vals:
        raise ValueError("empty")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def sp500_returns(obs: list[tuple[str, float]]) -> dict[str, Any] | None:
    """Annualized-ish simple stats on daily % changes if series long enough."""
    if len(obs) < 30:
        return None
    rets = []
    for i in range(1, len(obs)):
        a, b = obs[i - 1][1], obs[i][1]
        if a > 0:
            rets.append((b / a) - 1.0)
    if len(rets) < 20:
        return None
    s = sorted(rets)
    # Approx annualize mean daily * 252 for context only
    mean_d = statistics.mean(rets)
    return {
        "n_daily_returns": len(rets),
        "mean_daily_pct": round(mean_d * 100, 4),
        "median_daily_pct": round(statistics.median(rets) * 100, 4),
        "p05_daily_pct": round(percentile(s, 5) * 100, 4),
        "p95_daily_pct": round(percentile(s, 95) * 100, 4),
        "approx_ann_mean_pct": round(mean_d * 252 * 100, 2),
        "note_zh": "FRED SP500 日序列約由 2015 起；回報僅就可得樣本。",
    }


def normals_for(series_id: str, obs: list[tuple[str, float]]) -> dict[str, Any]:
    vals = [v for _, v in obs]
    s = sorted(vals)
    meta = SERIES_META[series_id]
    out: dict[str, Any] = {
        "id": series_id,
        "label_zh": meta["label_zh"],
        "label_en": meta["label_en"],
        "unit": meta["unit"],
        "fred_url": f"https://fred.stlouisfed.org/series/{series_id}",
        "start": obs[0][0],
        "end": obs[-1][0],
        "n_obs": len(obs),
        "mean": round(statistics.mean(vals), 6),
        "median": round(statistics.median(vals), 6),
        "stdev": round(statistics.pstdev(vals), 6) if len(vals) > 1 else 0.0,
        "p10": round(percentile(s, 10), 6),
        "p25": round(percentile(s, 25), 6),
        "p75": round(percentile(s, 75), 6),
        "p90": round(percentile(s, 90), 6),
        "min": round(s[0], 6),
        "max": round(s[-1], 6),
        "mean_fmt": fmt_value(series_id, statistics.mean(vals)),
        "median_fmt": fmt_value(series_id, statistics.median(vals)),
        "p10_fmt": fmt_value(series_id, percentile(s, 10)),
        "p90_fmt": fmt_value(series_id, percentile(s, 90)),
        "latest": {"date": obs[-1][0], "value": obs[-1][1], "value_fmt": fmt_value(series_id, obs[-1][1])},
    }
    # Trailing 10y window if possible
    end_d = parse_d(obs[-1][0])
    cut = (end_d - timedelta(days=365 * 10 + 5)).isoformat()
    trail = [v for d, v in obs if d >= cut]
    if len(trail) >= 24:
        ts = sorted(trail)
        out["trailing_10y"] = {
            "n_obs": len(trail),
            "start_approx": cut,
            "mean": round(statistics.mean(trail), 6),
            "median": round(statistics.median(trail), 6),
            "p10": round(percentile(ts, 10), 6),
            "p90": round(percentile(ts, 90), 6),
            "mean_fmt": fmt_value(series_id, statistics.mean(trail)),
            "median_fmt": fmt_value(series_id, statistics.median(trail)),
        }
    if series_id == "SP500":
        r = sp500_returns(obs)
        if r:
            out["returns"] = r
    return out


def snapshot_point(obs: list[tuple[str, float]], series_id: str, label: str, target: str) -> dict[str, Any]:
    # Monthly series need longer lag allowance
    monthly = {"UNRATE", "UMCSENT", "PERMIT", "INDPRO"}
    max_lag = 45 if series_id in monthly else 10
    if series_id in {"ICSA", "IC4WSA"}:
        max_lag = 14
    hit = nearest_on_or_before(obs, target, max_lag_days=max_lag)
    if hit is None:
        # Try with extended lag just to report availability
        hit2 = nearest_on_or_before(obs, target, max_lag_days=400)
        if hit2 is None:
            return {
                "anchor": label,
                "target_date": target,
                "available": False,
                "reason": "series_not_started_or_no_obs_before_date",
            }
        return {
            "anchor": label,
            "target_date": target,
            "available": False,
            "reason": "nearest_obs_too_stale",
            "nearest_date": hit2["date"],
            "nearest_value": hit2["value"],
            "lag_days": hit2["lag_days"],
        }
    stale = bool(hit.get("stale"))
    return {
        "anchor": label,
        "target_date": target,
        "available": True,
        "date": hit["date"],
        "value": hit["value"],
        "value_fmt": fmt_value(series_id, hit["value"]),
        "lag_days": hit["lag_days"],
        "stale": stale,
    }


def build_episode(ep: dict[str, Any], data: dict[str, list[tuple[str, float]]]) -> dict[str, Any]:
    series_out: dict[str, Any] = {}
    for sid in EPISODE_SERIES:
        obs = data.get(sid)
        meta = SERIES_META[sid]
        if not obs:
            series_out[sid] = {
                "id": sid,
                "label_zh": meta["label_zh"],
                "available": False,
                "reason": "fetch_failed",
            }
            continue
        start = obs[0][0]
        # Did series exist before "before" anchor?
        existed = parse_d(start) <= parse_d(ep["before"])
        points = {
            "before_6_12m": snapshot_point(obs, sid, "before", ep["before"]),
            "at_peak_stress": snapshot_point(obs, sid, "peak_stress", ep["peak_stress"]),
            "at_trough": snapshot_point(obs, sid, "trough", ep["trough"]),
            "after_6_12m": snapshot_point(obs, sid, "after", ep["after"]),
        }
        series_out[sid] = {
            "id": sid,
            "label_zh": meta["label_zh"],
            "label_en": meta["label_en"],
            "unit": meta["unit"],
            "series_start": start,
            "existed_at_before_anchor": existed,
            "points": points,
            "note_zh": None if existed else f"序列約由 {start} 起；此事件早期可能無資料。",
        }
    return {
        "id": ep["id"],
        "name_zh": ep["name_zh"],
        "name_en": ep["name_en"],
        "kind": ep["kind"],
        "nber_recession": ep["nber_recession"],
        "notes_zh": ep["notes_zh"],
        "anchors": {
            "before": ep["before"],
            "peak_stress": ep["peak_stress"],
            "trough": ep["trough"],
            "after": ep["after"],
        },
        "series": series_out,
    }


def main() -> int:
    api_key = os.environ.get("FRED_API_KEY", "").strip() or None
    # Optional fallback for local builds when CSV endpoint is unreachable:
    # a key may be passed via env only — never hardcode secrets in repo.
    fetch_mode_hint = "fred_api" if api_key else "fred_csv_keyless"

    all_ids = list(SERIES_META.keys())
    data: dict[str, list[tuple[str, float]]] = {}
    modes: dict[str, str] = {}
    errors: list[str] = []

    for sid in all_ids:
        try:
            obs, mode = fetch_series(sid, api_key)
            data[sid] = obs
            modes[sid] = mode
            print(f"OK {sid}: n={len(obs)} {obs[0][0]}→{obs[-1][0]} ({mode})", file=sys.stderr)
        except Exception as e:
            errors.append(f"{sid}: {e}")
            print(f"FAIL {sid}: {e}", file=sys.stderr)

    if not data:
        print("No series fetched", file=sys.stderr)
        return 1

    normals = {sid: normals_for(sid, obs) for sid, obs in data.items()}
    episodes = [build_episode(ep, data) for ep in EPISODES]

    now = datetime.now(HKT)
    payload = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M HKT"),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "FRED",
        "source_note": "Federal Reserve Bank of St. Louis FRED. Values are observed series levels (nearest observation on/before each anchor). Not interpolated inventively.",
        "source_urls": {
            "fred": "https://fred.stlouisfed.org/",
            "nber_cycles": "https://www.nber.org/research/data/us-business-cycle-expansions-and-contractions",
        },
        "fetch_mode": fetch_mode_hint,
        "fetch_modes_by_series": modes,
        "methodology_zh": (
            "正常帶：全樣本 mean / median / p10–p90（及可得時近 10 年）。"
            "股災錨點：約 6–12 個月前、壓力高峰、低位／最差月、約 6–12 個月後；"
            "取目標日或之前最近一筆 FRED 觀察值。NBER 衰退日期見 source_urls。"
        ),
        "normals": normals,
        "episodes": episodes,
        "errors": errors,
        "fred_coverage_notes": FRED_COVERAGE_NOTES,
        "gaps_zh": [
            "BAMLH0A0HYM2（HY OAS）：FRED 自約 2026-04 起只保留約 3 年；更早股災無 FRED 數值可取。長歷史信用利差改用 BAA10Y。",
            "SP500：FRED 日線約僅 10 年；1987／2000／2007 事件無 FRED SPX 水平可取。",
        ],
    }

    DOCS.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT} ({OUT.stat().st_size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
