"""Output JSON writer + signal bridge per il Decision Logic Engine.

Pipeline per ogni location:
  1. build_signals(pred, row, obs_summary) → SignalBag        [per ogni giorno]
  2. indicators.evaluate_all(signals) → list[IndicatorResult] [per ogni giorno]
  3. compute_coverage_30d(db, location_id) → dict             [una volta per location]
  4. write_location_json(location_id, days, coverage, ...) → Path

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


def _dewpoint(t: float, rh: float) -> float:
    """Punto di rugiada (°C) via formula di Magnus."""
    a, b = 17.625, 243.04
    gamma = math.log(rh / 100.0) + a * t / (b + t)
    return round(b * gamma / (a - gamma), 1)


def _apparent_temp(t: float, rh: float, ws: float) -> float:
    """Temperatura apparente Steadman/BoM (°C). ws in m/s."""
    e = (rh / 100.0) * 6.105 * math.exp(17.27 * t / (237.7 + t))
    return round(t + 0.33 * e - 0.70 * ws - 4.00, 1)

SignalBag = dict[str, float | None]

_NWP_WIND_COLS = [
    "ecmwf_wind_ms", "icon_wind_ms", "icond2_wind_ms",
    "gfs_wind_ms",   "arome_wind_ms", "icon2i_wind_ms",
]
_NWP_HUM_COLS = [
    "ecmwf_humidity_pct", "icon_humidity_pct", "icond2_humidity_pct",
    "gfs_humidity_pct",   "arome_humidity_pct", "icon2i_humidity_pct",
]

# Ordine e label per il confronto modelli nel frontend
_MODEL_ORDER = [
    "open_meteo_ecmwf_ifs",
    "open_meteo_icon_eu",
    "open_meteo_icon_d2",
    "open_meteo_gfs025",
    "open_meteo_arome_france",
    "open_meteo_italia_meteo_arpae_icon_2i",
]
_MODEL_LABELS: dict[str, str] = {
    "open_meteo_ecmwf_ifs":                  "ECMWF IFS",
    "open_meteo_icon_eu":                    "ICON-EU",
    "open_meteo_icon_d2":                    "ICON-D2",
    "open_meteo_gfs025":                     "GFS 0.25°",
    "open_meteo_arome_france":               "AROME",
    "open_meteo_italia_meteo_arpae_icon_2i": "ICON-2I",
}


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


def get_nwp_model_comparison(
    db: DuckDBClient,
    location_id: str,
    target_date: str,
) -> list[dict[str, Any]]:
    """Aggregato giornaliero per modello NWP: tmin, tmax, precip.

    Prende l'ultimo ts_run per ogni (source, ora) e aggrega la giornata intera.
    Modelli senza dati temp vengono omessi. Risultato ordinato per _MODEL_ORDER.

    Returns:
        Lista di {source, label, tmin_c, tmax_c, precip_mm}.
    """
    df = db.execute("""
        SELECT
            source,
            ROUND(MIN(temp_c), 1)                   AS tmin_c,
            ROUND(MAX(temp_c), 1)                   AS tmax_c,
            ROUND(SUM(COALESCE(precip_mm, 0.0)), 1) AS precip_mm
        FROM (
            SELECT source, ts_valid, temp_c, precip_mm
            FROM forecasts
            WHERE location_id = ?
              AND CAST(ts_valid AS DATE) = ?
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY source, ts_valid ORDER BY ts_run DESC
            ) = 1
        ) latest
        WHERE temp_c IS NOT NULL
        GROUP BY source
    """, [location_id, target_date]).df()

    if df.empty:
        return []

    by_source = {row["source"]: row for _, row in df.iterrows()}

    return [
        {
            "source":    src,
            "label":     _MODEL_LABELS.get(src, src),
            "tmin_c":    _get(by_source[src], "tmin_c"),
            "tmax_c":    _get(by_source[src], "tmax_c"),
            "precip_mm": _get(by_source[src], "precip_mm"),
        }
        for src in _MODEL_ORDER
        if src in by_source
    ]


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


def build_signals_today(
    pred: dict[str, dict[str, float]],
    row: pd.Series,
    obs_summary: dict[str, float | None] | None = None,
    current_obs: dict[str, float | None] | None = None,
) -> SignalBag:
    """Come build_signals, ma sostituisce i segnali osservabili con i valori realtime.

    Per oggi i segnali di precipitazione, vento e umidità diventano deterministici
    (0.0 o 1.0) se current_obs è disponibile. Temperatura minima e gelata restano
    dalle previsioni ML perché non sappiamo ancora il minimo della notte.

    Args:
        current_obs: output di get_current_conditions() — {temp_c, humidity_pct,
                     precip_mm, wind_speed_ms}. Se None, si usa build_signals puro.
    """
    signals = build_signals(pred, row, obs_summary)
    if not current_obs:
        return signals

    prec = current_obs.get("precip_mm") or 0.0
    temp = current_obs.get("temp_c")
    wind = current_obs.get("wind_speed_ms")
    hum  = current_obs.get("humidity_pct")

    signals["P(precip > 0.2mm)"] = 1.0 if prec >= 0.2  else 0.0
    signals["P(precip > 3mm)"]   = 1.0 if prec >= 3.0  else 0.0
    signals["P(precip > 5mm/h)"] = 1.0 if prec >= 5.0  else 0.0

    if temp is not None:
        signals["T2m_p50"] = temp

    if wind is not None:
        signals["P(wind > 40kmh)"] = 1.0 if wind > 40 / 3.6 else 0.0
        signals["P(wind < 5kmh)"]  = 1.0 if wind < 5  / 3.6 else 0.0

    if hum is not None:
        signals["P(RH > 80%)"] = 1.0 if hum > 80 else 0.0
        signals["P(RH > 95% AND wind < 3kmh)"] = (
            1.0 if (hum > 95 and wind is not None and wind < 3 / 3.6) else 0.0
        )

    return signals


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


def get_current_conditions(
    db: DuckDBClient,
    location_id: str,
) -> dict[str, Any] | None:
    """Ultima lettura realtime aggregata per una location (media stazioni, ultimi 3h).

    Returns:
        {ts, temp_c, humidity_pct, precip_mm, wind_speed_ms,
         dewpoint_c, feels_like_c} oppure None se non ci sono osservazioni
        recenti con temperatura disponibile.
    """
    row = db.execute("""
        SELECT
            strftime(MAX(ts), '%Y-%m-%dT%H:%M:%S') || '+00:00' AS ts,
            ROUND(AVG(temp_c), 1)                               AS temp_c,
            ROUND(AVG(humidity_pct), 0)                         AS humidity_pct,
            ROUND(SUM(COALESCE(precip_mm, 0.0)), 2)             AS precip_mm,
            ROUND(AVG(wind_speed_ms), 1)                        AS wind_speed_ms
        FROM observations
        WHERE location_id = ?
          AND granularity = 'realtime'
          AND ts >= NOW() - INTERVAL 3 HOURS
          AND temp_c IS NOT NULL
    """, [location_id]).fetchone()

    if row is None or row[1] is None:
        return None

    ts, temp_c, humidity_pct, precip_mm, wind_speed_ms = row
    t    = float(temp_c)
    rh   = float(humidity_pct) if humidity_pct is not None else None
    ws   = float(wind_speed_ms) if wind_speed_ms is not None else None

    dew      = _dewpoint(t, rh) if rh is not None else None
    apparent = _apparent_temp(t, rh, ws if ws is not None else 0.0) if rh is not None else None

    return {
        "ts":            ts,
        "temp_c":        t,
        "humidity_pct":  rh,
        "precip_mm":     float(precip_mm) if precip_mm is not None else None,
        "wind_speed_ms": ws,
        "dewpoint_c":    dew,
        "feels_like_c":  apparent,
    }


def get_today_hourly(
    db: DuckDBClient,
    location_id: str,
) -> list[dict[str, Any]] | None:
    """Profilo orario NWP ensemble per le ore rimanenti di oggi (no rescaling ML).

    Returns:
        Lista di dict {hour, temp_c, humidity_pct, precip_mm, precip_prob} per le
        ore future di oggi, oppure None se vuoto.
    """
    df = db.execute("""
        SELECT
            HOUR(ts_valid)                                                   AS hour,
            ROUND(AVG(temp_c), 1)                                            AS temp_c,
            ROUND(AVG(humidity_pct), 0)                                      AS humidity_pct,
            ROUND(AVG(COALESCE(precip_mm, 0.0)), 2)                          AS precip_mm,
            ROUND(AVG(CASE WHEN precip_mm IS NULL THEN NULL
                           WHEN precip_mm > 0.1   THEN 1.0
                           ELSE 0.0 END), 2)                                 AS precip_prob,
            ROUND(AVG(wind_speed_ms), 1)                                     AS wind_speed_ms
        FROM (
            SELECT source, ts_valid, temp_c, humidity_pct, precip_mm, wind_speed_ms
            FROM forecasts
            WHERE location_id = ?
              AND CAST(ts_valid AS DATE) = CURRENT_DATE
              AND ts_valid > NOW()
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY source, ts_valid ORDER BY ts_run DESC
            ) = 1
        ) latest
        WHERE temp_c IS NOT NULL
        GROUP BY hour
        ORDER BY hour
    """, [location_id]).df()

    if df.empty:
        return None

    return [
        {
            "hour":          int(r["hour"]),
            "temp_c":        float(r["temp_c"]) if r["temp_c"] is not None else None,
            "humidity_pct":  float(r["humidity_pct"]) if r["humidity_pct"] is not None else None,
            "precip_mm":     float(r["precip_mm"]) if r["precip_mm"] is not None else None,
            "precip_prob":   float(r["precip_prob"]) if r["precip_prob"] is not None else None,
            "wind_speed_ms": float(r["wind_speed_ms"]) if r["wind_speed_ms"] is not None else None,
        }
        for _, r in df.iterrows()
    ]


def get_nwp_models_hourly(
    db: DuckDBClient,
    location_id: str,
) -> list[dict[str, Any]]:
    """Serie orarie per-modello NWP (ore future), per lo switch grafico frontend.

    Returns:
        Lista di {source, label, data: [{ts, temp_c, humidity_pct, precip_mm}]}
        ordinata per _MODEL_ORDER. Modelli senza dati vengono omessi.
    """
    df = db.execute("""
        SELECT
            source,
            strftime(ts_valid, '%Y-%m-%dT%H:%M:%SZ')   AS ts,
            ROUND(temp_c, 1)                             AS temp_c,
            ROUND(humidity_pct, 0)                       AS humidity_pct,
            ROUND(COALESCE(precip_mm, 0.0), 2)           AS precip_mm,
            ROUND(wind_speed_ms, 1)                      AS wind_speed_ms
        FROM forecasts
        WHERE location_id = ?
          AND ts_valid > NOW()
          AND temp_c IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY source, ts_valid ORDER BY ts_run DESC
        ) = 1
        ORDER BY source, ts_valid
    """, [location_id]).df()

    if df.empty:
        return []

    by_source: dict[str, list[dict[str, Any]]] = {}
    for _, r in df.iterrows():
        src = str(r["source"])
        by_source.setdefault(src, []).append({
            "ts":            str(r["ts"]),
            "temp_c":        float(r["temp_c"]) if r["temp_c"] is not None else None,
            "humidity_pct":  float(r["humidity_pct"]) if r["humidity_pct"] is not None else None,
            "precip_mm":     float(r["precip_mm"]) if r["precip_mm"] is not None else None,
            "wind_speed_ms": float(r["wind_speed_ms"]) if r["wind_speed_ms"] is not None else None,
        })

    return [
        {"source": src, "label": _MODEL_LABELS.get(src, src), "data": by_source[src]}
        for src in _MODEL_ORDER
        if src in by_source
    ]


def compute_hourly_profile(
    db: DuckDBClient,
    location_id: str,
    target_date: str,
    tmin_p50: float | None,
    tmax_p50: float | None,
    precip_p50: float | None,
) -> list[dict[str, float | None]] | None:
    """Profilo orario disaggregato da NWP ensemble, ancorato alle previsioni ML.

    Temperatura: rescaling lineare del profilo ensemble-mean da [raw_min, raw_max]
    a [tmin_p50, tmax_p50]. Se tmin_p50/tmax_p50 sono None usa i valori raw.

    Precipitazione: distribuzione oraria NWP scalata proporzionalmente così che la
    somma giornaliera corrisponda a precip_p50 ML. precip_prob = frazione modelli
    con precip > 0.1mm/h per quell'ora.

    Returns:
        Lista di 24 dict {hour, temp_c, humidity_pct, precip_mm, precip_prob} oppure
        None se non ci sono dati NWP per il giorno richiesto.
    """
    df = db.execute("""
        SELECT
            HOUR(ts_valid)                                                      AS hour,
            AVG(temp_c)                                                         AS temp_mean,
            AVG(humidity_pct)                                                   AS humidity_mean,
            AVG(COALESCE(precip_mm, 0.0))                                       AS precip_mean,
            AVG(CASE WHEN precip_mm IS NULL THEN NULL
                     WHEN precip_mm > 0.1   THEN 1.0
                     ELSE 0.0 END)                                              AS precip_prob,
            AVG(wind_speed_ms)                                                  AS wind_mean
        FROM (
            SELECT source, ts_valid, temp_c, humidity_pct, precip_mm, wind_speed_ms
            FROM forecasts
            WHERE location_id = ?
              AND CAST(ts_valid AS DATE) = ?
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY source, ts_valid
                ORDER BY ts_run DESC
            ) = 1
        ) latest
        WHERE temp_c IS NOT NULL
        GROUP BY hour
        ORDER BY hour
    """, [location_id, target_date]).df()

    if df.empty:
        return None

    hour_data: dict[int, tuple[float, float | None, float, float | None, float | None]] = {
        int(r["hour"]): (
            float(r["temp_mean"]),
            float(r["humidity_mean"]) if r["humidity_mean"] is not None else None,
            float(r["precip_mean"]),
            float(r["precip_prob"]) if r["precip_prob"] is not None else None,
            float(r["wind_mean"]) if r["wind_mean"] is not None else None,
        )
        for _, r in df.iterrows()
    }

    raw_temps = [v for _, (v, _, _, _, _) in sorted(hour_data.items())]
    raw_min = min(raw_temps)
    raw_max = max(raw_temps)

    def _rescale_temp(v: float) -> float:
        if tmin_p50 is None or tmax_p50 is None:
            return round(v, 1)
        span_raw = raw_max - raw_min
        if span_raw <= 0:
            return round((tmin_p50 + tmax_p50) / 2.0, 1)
        return round(tmin_p50 + (v - raw_min) / span_raw * (tmax_p50 - tmin_p50), 1)

    total_precip_raw = sum(v for _, (_, _, v, _, _) in hour_data.items())
    if total_precip_raw > 0 and precip_p50 is not None and precip_p50 > 0:
        precip_scale = precip_p50 / total_precip_raw
    else:
        precip_scale = 0.0

    result: list[dict[str, float | None]] = []
    for h in range(24):
        if h in hour_data:
            t_raw, hum, p_raw, prob, wind = hour_data[h]
            result.append({
                "hour":          h,
                "temp_c":        _rescale_temp(t_raw),
                "humidity_pct":  round(hum, 0) if hum is not None else None,
                "precip_mm":     round(p_raw * precip_scale, 2),
                "precip_prob":   round(prob, 2) if prob is not None else None,
                "wind_speed_ms": round(wind, 1) if wind is not None else None,
            })
        else:
            result.append({
                "hour": h, "temp_c": None, "humidity_pct": None,
                "precip_mm": None, "precip_prob": None, "wind_speed_ms": None,
            })

    return result


def write_location_json(
    location_id: str,
    days: list[dict[str, Any]],
    coverage: dict[str, float | None],
    output_dir: Path,
    db: DuckDBClient | None = None,
) -> Path:
    """Scrive il JSON di output per una location con previsioni multi-giorno.

    Args:
        days: lista ordinata per data, ogni elemento:
              {target_date: str, lead_time_h: int,
               pred: {target: {quantile: float}},
               indicators: list[IndicatorResult]}
        db:   se fornito, aggiunge current, today_hourly, nwp_models_hourly al payload

    Struttura JSON:
      {location_id, generated_at, coverage_empirical_30d,
       current?, today_hourly?, nwp_models_hourly?,
       days: [{target_date, lead_time_h, forecasts, indicators, hourly}, ...]}

    Returns:
        Path del file scritto.
    """
    def _fmt_target(t: dict[str, float]) -> dict[str, float | None]:
        return {
            "p50":     t.get("p50"),
            "ci80_lo": t.get("ci80_lo"), "ci80_hi": t.get("ci80_hi"),
            "ci90_lo": t.get("ci90_lo"), "ci90_hi": t.get("ci90_hi"),
        }

    def _fmt_precip(t: dict[str, float]) -> dict[str, float | None]:
        # Quantile regression può restituire valori leggermente negativi vicino a 0.
        # Clamp fisico: la precipitazione non può essere negativa.
        def _c(v: float | None) -> float | None:
            return max(0.0, v) if v is not None else None
        return {
            "p50":     _c(t.get("p50")),
            "ci80_lo": _c(t.get("ci80_lo")), "ci80_hi": _c(t.get("ci80_hi")),
            "ci90_lo": _c(t.get("ci90_lo")), "ci90_hi": _c(t.get("ci90_hi")),
        }

    day_payloads = []
    for day in days:
        pred: dict[str, dict[str, float]] = day["pred"]
        inds: list[IndicatorResult] = day["indicators"]
        day_payloads.append({
            "target_date": day["target_date"],
            "lead_time_h": day["lead_time_h"],
            "forecasts": {
                "tmin_c":    _fmt_target(pred.get("tmin_c",    {})),
                "tmax_c":    _fmt_target(pred.get("tmax_c",    {})),
                "precip_mm": _fmt_precip(pred.get("precip_mm", {})),
            },
            "indicators": {
                r.indicator_id: {"verdict": r.verdict, "rule_matched": r.rule_matched}
                for r in inds
            },
            "hourly":          day.get("hourly"),
            "nwp_comparison":  day.get("nwp_comparison"),
        })

    payload: dict[str, Any] = {
        "location_id":            location_id,
        "generated_at":           datetime.now(tz=UTC).isoformat(),
        "coverage_empirical_30d": coverage,
    }
    if db is not None:
        payload["current"]            = get_current_conditions(db, location_id)
        payload["today_hourly"]       = get_today_hourly(db, location_id)
        payload["nwp_models_hourly"]  = get_nwp_models_hourly(db, location_id)
    payload["days"] = day_payloads

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{location_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path
