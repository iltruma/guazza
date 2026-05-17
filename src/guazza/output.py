"""Output JSON writer + signal bridge per il Decision Logic Engine.

Pipeline per ogni location:
  1. build_signals(pred, row, obs_summary) → SignalBag
  2. indicators.evaluate_all(signals) → list[IndicatorResult]
  3. compute_coverage_30d(db, location_id) → dict
  4. write_location_json(...) → Path

SignalBag contract (chiavi riconosciute dal DLE):
  Precipitazione  — P(precip > 0.2mm), P(precip > 3mm), P(precip > 5mm/h)
  Temperatura     — P(Tmin < 2.0°C), P(Tmin < 0.0°C), Tmin_p10, T2m_p50
  Vento NWP       — P(wind > 40kmh), P(wind < 5kmh)
  Umidità NWP     — P(RH > 80%), P(RH > 95% AND wind < 3kmh)
  Real-time obs   — level_sir, pm10_predicted
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

if TYPE_CHECKING:
    from guazza.indicators import IndicatorResult
    from guazza.storage import DuckDBClient

SignalBag = dict[str, float | None]

_NWP_WIND_COLS = [
    "ecmwf_wind_ms", "icon_wind_ms", "icond2_wind_ms",
    "gfs_wind_ms",   "arome_wind_ms", "icon2i_wind_ms",
]
_NWP_HUM_COLS = [
    "ecmwf_humidity_pct", "icon_humidity_pct", "icond2_humidity_pct",
    "gfs_humidity_pct",   "arome_humidity_pct", "icon2i_humidity_pct",
]


def _get(row: pd.Series, col: str) -> float | None:
    """Legge un valore dalla Series, restituendo None per NaN."""
    v = row.get(col)
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _prob_exceeds(q: dict[str, float], threshold: float) -> float:
    """P(X > threshold) da quantile predictions con interpolazione lineare.

    Usa i quantili p05/p10/p50/p90/p95. Estrapolazione ai bordi: se threshold
    cade fuori dall'intervallo, usa il quantile estremo come limite.
    """
    points = sorted(
        ((float(k[1:]) / 100, v) for k, v in q.items() if k.startswith("p") and k[1:].isdigit()),
        key=lambda p: p[1],
    )
    if not points:
        return 0.5
    if threshold <= points[0][1]:
        return 1.0 - points[0][0]
    if threshold >= points[-1][1]:
        return 1.0 - points[-1][0]
    for i in range(len(points) - 1):
        q_lo, v_lo = points[i]
        q_hi, v_hi = points[i + 1]
        if v_lo <= threshold <= v_hi:
            t = (threshold - v_lo) / (v_hi - v_lo) if v_hi != v_lo else 0.5
            return 1.0 - (q_lo + t * (q_hi - q_lo))
    return 0.5


def _nwp_frac(vals: list[float | None], pred: Any) -> float:
    """Frazione di modelli NWP per cui pred(v) è True. 0.5 se nessun modello disponibile."""
    valid = [v for v in vals if v is not None]
    if not valid:
        return 0.5
    return sum(1 for v in valid if pred(v)) / len(valid)


def build_signals(
    pred: dict[str, dict[str, float]],
    row: pd.Series,
    obs_summary: dict[str, float | None] | None = None,
) -> SignalBag:
    """Costruisce il SignalBag per il DLE da predizioni ML + NWP ensemble + obs.

    Args:
        pred:        output di models.predict() — {target: {quantile_key: float}}
        row:         riga di features_daily come pandas Series (contiene colonne NWP)
        obs_summary: valori real-time: level_sir, pm10_predicted (entrambi opzionali)
    """
    obs = obs_summary or {}
    precip_q = pred.get("precip_mm", {})
    tmin_q   = pred.get("tmin_c",   {})
    tmax_q   = pred.get("tmax_c",   {})

    wind_vals: list[float | None] = [_get(row, c) for c in _NWP_WIND_COLS]
    hum_vals:  list[float | None] = [_get(row, c) for c in _NWP_HUM_COLS]

    # P(nebbia) = P(RH > 95% AND wind < 3km/h): prob congiunta da ensemble NWP
    hw_pairs = [
        (h, w) for h, w in zip(hum_vals, wind_vals, strict=False)
        if h is not None and w is not None
    ]
    p_nebbia = (
        sum(1 for h, w in hw_pairs if h > 95 and w < 3 / 3.6) / len(hw_pairs)
        if hw_pairs else 0.5
    )

    return {
        # Precipitazione (ML quantile → CDF inversa lineare)
        "P(precip > 0.2mm)": _prob_exceeds(precip_q, 0.2),
        "P(precip > 3mm)":   _prob_exceeds(precip_q, 3.0),
        "P(precip > 5mm/h)": _prob_exceeds(precip_q, 5.0),  # proxy: daily mm ≈ intensità

        # Temperatura minima (ML quantile)
        "P(Tmin < 2.0°C)": 1.0 - _prob_exceeds(tmin_q, 2.0),
        "P(Tmin < 0.0°C)": 1.0 - _prob_exceeds(tmin_q, 0.0),
        "Tmin_p10":         tmin_q.get("p10"),

        # Temperatura 2m (proxy: tmax p50 rappresenta la temperatura diurna)
        "T2m_p50": tmax_q.get("p50"),

        # Vento (NWP ensemble, da m/s a km/h in soglia)
        "P(wind > 40kmh)": _nwp_frac(wind_vals, lambda v: v > 40 / 3.6),
        "P(wind < 5kmh)":  _nwp_frac(wind_vals, lambda v: v < 5  / 3.6),

        # Umidità (NWP ensemble)
        "P(RH > 80%)":                  _nwp_frac(hum_vals, lambda v: v > 80),
        "P(RH > 95% AND wind < 3kmh)": p_nebbia,

        # Real-time obs (opzionali — None se non disponibili)
        "level_sir":      obs.get("level_sir"),
        "pm10_predicted": obs.get("pm10_predicted"),
    }


def compute_coverage_30d(
    db: DuckDBClient,
    location_id: str,
    min_samples: int = 10,
) -> dict[str, float | None]:
    """Copertura empirica rolling 30 giorni: frazione di osservazioni dentro il CI.

    Richiede predictions con *_obs popolato (via backfill_prediction_obs).
    Restituisce null per tutti i target se < min_samples campioni disponibili.

    Returns:
        {tmin_ci80, tmin_ci90, tmax_ci80, tmax_ci90, precip_ci80, precip_ci90}
    """
    null_result: dict[str, float | None] = {
        "tmin_ci80": None, "tmin_ci90": None,
        "tmax_ci80": None, "tmax_ci90": None,
        "precip_ci80": None, "precip_ci90": None,
    }

    df = db.execute("""
        SELECT
            tmin_ci80_lo, tmin_ci80_hi, tmin_ci90_lo, tmin_ci90_hi, tmin_obs,
            tmax_ci80_lo, tmax_ci80_hi, tmax_ci90_lo, tmax_ci90_hi, tmax_obs,
            precip_ci80_lo, precip_ci80_hi, precip_ci90_lo, precip_ci90_hi, precip_obs
        FROM predictions
        WHERE location_id = ?
          AND ts_valid >= CURRENT_TIMESTAMP - INTERVAL 30 DAYS
          AND tmin_obs IS NOT NULL
    """, [location_id]).df()

    if len(df) < min_samples:
        return null_result

    def _cov(lo: str, hi: str, obs: str) -> float | None:
        mask = df[lo].notna() & df[hi].notna() & df[obs].notna()
        if mask.sum() < min_samples:
            return None
        inside = ((df.loc[mask, obs] >= df.loc[mask, lo]) &
                  (df.loc[mask, obs] <= df.loc[mask, hi]))
        return float(inside.mean())

    return {
        "tmin_ci80":   _cov("tmin_ci80_lo",   "tmin_ci80_hi",   "tmin_obs"),
        "tmin_ci90":   _cov("tmin_ci90_lo",   "tmin_ci90_hi",   "tmin_obs"),
        "tmax_ci80":   _cov("tmax_ci80_lo",   "tmax_ci80_hi",   "tmax_obs"),
        "tmax_ci90":   _cov("tmax_ci90_lo",   "tmax_ci90_hi",   "tmax_obs"),
        "precip_ci80": _cov("precip_ci80_lo", "precip_ci80_hi", "precip_obs"),
        "precip_ci90": _cov("precip_ci90_lo", "precip_ci90_hi", "precip_obs"),
    }


def write_location_json(
    location_id: str,
    target_date: str,
    lead_time_h: int,
    pred: dict[str, dict[str, float]],
    indicators: list[IndicatorResult],
    coverage: dict[str, float | None],
    output_dir: Path,
) -> Path:
    """Scrive il JSON di output per una location.

    Struttura:
      location_id, generated_at, target_date, lead_time_h,
      forecasts: {tmin_c, tmax_c, precip_mm} × {p50, ci80_lo/hi, ci90_lo/hi},
      indicators: {id: {verdict, rule_matched}},
      coverage_empirical_30d: {tmin_ci80, ..., precip_ci90}

    Returns:
        Path del file scritto.
    """
    def _fmt_target(t: dict[str, float]) -> dict[str, float | None]:
        return {
            "p50":    t.get("p50"),
            "ci80_lo": t.get("ci80_lo"), "ci80_hi": t.get("ci80_hi"),
            "ci90_lo": t.get("ci90_lo"), "ci90_hi": t.get("ci90_hi"),
        }

    payload: dict[str, Any] = {
        "location_id":  location_id,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "target_date":  target_date,
        "lead_time_h":  lead_time_h,
        "forecasts": {
            "tmin_c":    _fmt_target(pred.get("tmin_c",   {})),
            "tmax_c":    _fmt_target(pred.get("tmax_c",   {})),
            "precip_mm": _fmt_target(pred.get("precip_mm", {})),
        },
        "indicators": {
            r.indicator_id: {"verdict": r.verdict, "rule_matched": r.rule_matched}
            for r in indicators
        },
        "coverage_empirical_30d": coverage,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{location_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path
