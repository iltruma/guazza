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
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger

if TYPE_CHECKING:
    from guazza.indicators import IndicatorResult
    from guazza.storage import DuckDBClient


def expected_precip(q: Mapping[str, float | None]) -> float | None:
    """Valore atteso E[X] della distribuzione predittiva via quadratura trapezoidale.

    Usa i 5 quantili (p05/p10/p50/p90/p95) con estremi rettangolari:
    Q(0) = Q(0.05), Q(1) = Q(0.95). Restituisce None se mancano dati;
    risultato clampato a 0 (precip non negativa).
    """
    alphas = [0.05, 0.10, 0.50, 0.90, 0.95]
    keys   = ["p05", "p10", "p50", "p90", "p95"]
    vals: list[float] = []
    for k in keys:
        v = q.get(k)
        if v is None:
            return None
        vals.append(v)
    # Aggiunge estremi rettangolari: α=0 → Q(0.05), α=1 → Q(0.95)
    ext_a = [0.0] + alphas + [1.0]
    ext_v = [vals[0]] + vals + [vals[-1]]
    total = 0.0
    for i in range(len(ext_a) - 1):
        total += (ext_a[i + 1] - ext_a[i]) * (ext_v[i] + ext_v[i + 1]) / 2.0
    return max(0.0, total)


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
    """Aggregato giornaliero per modello NWP: tmin, tmax, precip, ultimo ts_run.

    Prende l'ultimo ts_run per ogni (source, ora) e aggrega la giornata intera.
    Modelli senza dati temp vengono omessi. Risultato ordinato per _MODEL_ORDER.

    Returns:
        Lista di {source, label, tmin_c, tmax_c, precip_mm, last_run}.
    """
    df = db.execute("""
        SELECT
            source,
            ROUND(MIN(temp_c), 1)                           AS tmin_c,
            ROUND(MAX(temp_c), 1)                           AS tmax_c,
            ROUND(SUM(COALESCE(precip_mm, 0.0)), 1)         AS precip_mm,
            strftime(MAX(ts_run), '%Y-%m-%dT%H:%M:%SZ')     AS last_run
        FROM (
            SELECT source, ts_valid, temp_c, precip_mm, ts_run
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
            "last_run":  str(by_source[src]["last_run"]) if by_source[src]["last_run"] is not None else None,
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


def get_current_air_quality(
    db: DuckDBClient,
    location_id: str,
) -> dict[str, float | None] | None:
    """Ultimi valori qualità aria ARPAT NRT per una location (finestra 6h, solo orario).

    Finestra 6h invece di 3h: ARPAT pubblica con 2-3h di ritardo sull'ora corrente.
    Con il cron ogni 30min, senza margine sufficiente i valori sparirebbero tra un fetch
    e il successivo aggiornamento ARPAT.

    Le stazioni sono risolte via JOIN su station_weights (source='arpat'), non su
    observations.location_id: la PK observations non include location_id, quindi una
    stazione condivisa tra location porta un solo tag arbitrario. Media pesata per
    stazione (peso dal config), stessa logica del blend SIR. Richiede station_weights
    popolata da `weights refresh`.

    Returns:
        {pm10_ugm3, pm25_ugm3, no2_ugm3, o3_ugm3, co_mgm3, benzene_ugm3, so2_ugm3}
        oppure None se nessun dato nella finestra.
    """
    row = db.execute("""
        WITH aq AS (
            SELECT o.pm10_ugm3, o.pm25_ugm3, o.no2_ugm3, o.o3_ugm3,
                   o.co_mgm3, o.benzene_ugm3, o.so2_ugm3, sw.weight AS w
            FROM observations o
            JOIN station_weights sw
              ON o.station_id = sw.station_id AND sw.source = 'arpat'
            WHERE sw.location_id = ?
              AND o.source = 'arpat'
              AND (
                  (o.granularity = 'hourly' AND o.ts >= CURRENT_TIMESTAMP - INTERVAL 6 HOURS)
               OR (o.granularity = 'daily'  AND o.ts >= CURRENT_TIMESTAMP - INTERVAL 3 DAYS)
              )
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY o.station_id, o.granularity ORDER BY o.ts DESC
            ) = 1
        )
        SELECT
            ROUND(SUM(pm10_ugm3 * w)    / NULLIF(SUM(CASE WHEN pm10_ugm3    IS NOT NULL THEN w ELSE 0 END), 0), 1) AS pm10_ugm3,
            ROUND(SUM(pm25_ugm3 * w)    / NULLIF(SUM(CASE WHEN pm25_ugm3    IS NOT NULL THEN w ELSE 0 END), 0), 1) AS pm25_ugm3,
            ROUND(SUM(no2_ugm3 * w)     / NULLIF(SUM(CASE WHEN no2_ugm3     IS NOT NULL THEN w ELSE 0 END), 0), 1) AS no2_ugm3,
            ROUND(SUM(o3_ugm3 * w)      / NULLIF(SUM(CASE WHEN o3_ugm3      IS NOT NULL THEN w ELSE 0 END), 0), 1) AS o3_ugm3,
            ROUND(SUM(co_mgm3 * w)      / NULLIF(SUM(CASE WHEN co_mgm3      IS NOT NULL THEN w ELSE 0 END), 0), 2) AS co_mgm3,
            ROUND(SUM(benzene_ugm3 * w) / NULLIF(SUM(CASE WHEN benzene_ugm3 IS NOT NULL THEN w ELSE 0 END), 0), 2) AS benzene_ugm3,
            ROUND(SUM(so2_ugm3 * w)     / NULLIF(SUM(CASE WHEN so2_ugm3     IS NOT NULL THEN w ELSE 0 END), 0), 1) AS so2_ugm3
        FROM aq
    """, [location_id]).fetchone()

    if row is None or all(v is None for v in row):
        return None

    pm10, pm25, no2, o3, co, benzene, so2 = row
    return {
        "pm10_ugm3":     float(pm10)    if pm10    is not None else None,
        "pm25_ugm3":     float(pm25)    if pm25    is not None else None,
        "no2_ugm3":      float(no2)     if no2     is not None else None,
        "o3_ugm3":       float(o3)      if o3      is not None else None,
        "co_mgm3":       float(co)      if co      is not None else None,
        "benzene_ugm3":  float(benzene) if benzene is not None else None,
        "so2_ugm3":      float(so2)     if so2     is not None else None,
    }


# Severità WMO per il tie-break quando più codici hanno la stessa frequenza modale.
# Valori più alti = condizione più severa. Gruppi di codici raggruppati per classe WMO.
_WMO_SEVERITY: dict[int, int] = {
    0: 0, 1: 1, 2: 2, 3: 3,
    45: 4, 48: 5,
    51: 6, 53: 7, 55: 8,
    56: 9, 57: 10,
    61: 11, 63: 12, 65: 13,
    66: 14, 67: 15,
    71: 16, 73: 17, 75: 18, 77: 19,
    80: 20, 81: 21, 82: 22,
    85: 23, 86: 24,
    95: 25,
    96: 26, 99: 27,
}


def _modal_weather_code(codes: list[int]) -> int | None:
    """Codice WMO modale da una lista di interi.

    In caso di pareggio, vince il codice con severità più alta secondo _WMO_SEVERITY.
    Codici sconosciuti ottengono severità 0.
    """
    if not codes:
        return None
    counter = Counter(codes)
    max_freq = max(counter.values())
    candidates = [c for c, f in counter.items() if f == max_freq]
    # tie-break: severità più alta
    return max(candidates, key=lambda c: _WMO_SEVERITY.get(c, 0))


_MIN_HOURS_FOR_DAILY_CODE = 20  # source con meno ore esclusa dal consenso
_MIN_CODE_HOURS_FOR_SEVERITY = 2  # soglia minima ore per considerare un codice "reale"


def _pessimistic_weather_code(codes: list[int]) -> int | None:
    """Codice WMO pessimistico: il più severo che appare in almeno _MIN_CODE_HOURS_FOR_SEVERITY ore.

    Filtra spike singoli (1 ora outlier) ma mostra pioggia/temporale se si ripete ≥2 ore.
    Fallback alla severità massima assoluta se tutti i codici appaiono meno di 2 volte.
    """
    if not codes:
        return None
    counter = Counter(codes)
    stable = [c for c, n in counter.items() if n >= _MIN_CODE_HOURS_FOR_SEVERITY]
    candidates = stable if stable else list(counter.keys())
    return max(candidates, key=lambda c: _WMO_SEVERITY.get(c, 0))


def get_daily_weather_code(
    db: DuckDBClient,
    location_id: str,
    target_date: str,
) -> int | None:
    """Codice WMO giornaliero per una location e data, caso pessimistico tra modelli.

    Algoritmo: per ogni (source, ts_valid) prende il run più recente, esclude le source
    con dati parziali (< _MIN_HOURS_FOR_DAILY_CODE ore), poi ritorna il codice di severità
    massima che appare in almeno _MIN_CODE_HOURS_FOR_SEVERITY ore.

    Returns:
        Codice WMO intero, o None se nessun dato disponibile.
    """
    rows = db.execute("""
        WITH latest AS (
            SELECT source, ts_valid, weather_code
            FROM forecasts
            WHERE location_id = ?
              AND CAST(ts_valid AS DATE) = ?
              AND weather_code IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY source, ts_valid ORDER BY ts_run DESC
            ) = 1
        ),
        full_sources AS (
            SELECT source
            FROM latest
            GROUP BY source
            HAVING COUNT(DISTINCT HOUR(ts_valid)) >= ?
        )
        SELECT l.weather_code
        FROM latest l
        JOIN full_sources fs USING (source)
    """, [location_id, target_date, _MIN_HOURS_FOR_DAILY_CODE]).fetchall()

    codes = [int(r[0]) for r in rows if r[0] is not None]
    return _pessimistic_weather_code(codes)


def get_current_conditions(
    db: DuckDBClient,
    location_id: str,
) -> dict[str, Any] | None:
    """Condizioni attuali per una location: media pesata per distanza delle
    stazioni intorno (ultimi 3h). Fallback al NWP Open-Meteo se mancano osservazioni.

    Un'osservazione appartiene a una stazione fisica, non a una location: il legame
    stazione→location e il peso (decay distanza × penalità quota) vivono in
    `station_weights` per SIR. Netatmo porta il proprio peso dinamico nella colonna
    `observations.weight`. I due rami vengono combinati in un'unica media pesata.

    Returns:
        {ts, temp_c, humidity_pct, precip_mm, wind_speed_ms,
         dewpoint_c, feels_like_c} oppure None se non ci sono né osservazioni
        recenti né forecast disponibili.
    """
    # Media pesata: SIR pesato via station_weights (JOIN su station_id, non
    # location_id — la riga obs è taggata con una sola location arbitraria),
    # Netatmo pesato via observations.weight. Una lettura per stazione (la più
    # recente nella finestra), poi blend con SUM(v*w)/SUM(w).
    blend_sql = """
        WITH netatmo_latest AS (
            -- Una lettura per modulo (la più recente nella finestra), già filtrata QC.
            SELECT o.ts, o.temp_c, o.humidity_pct, o.precip_mm,
                   o.wind_speed_ms, o.wind_dir_deg, o.weight
            FROM observations o
            WHERE o.source = 'netatmo'
              AND o.location_id = ?
              AND o.granularity = 'realtime'
              AND o.weight IS NOT NULL
              AND o.qc_pass
              AND o.ts >= NOW() - INTERVAL 3 HOURS
            QUALIFY ROW_NUMBER() OVER (PARTITION BY o.station_id ORDER BY o.ts DESC) = 1
        ),
        obs AS (
            SELECT 'sir' AS src, o.ts, o.temp_c, o.humidity_pct, o.precip_mm,
                   o.wind_speed_ms, o.wind_dir_deg, sw.weight AS w
            FROM observations o
            JOIN station_weights sw
              ON o.station_id = sw.station_id AND sw.source = 'sir'
            WHERE sw.location_id = ?
              AND o.source = 'sir_toscana'
              AND o.granularity = 'realtime'
              AND o.ts >= NOW() - INTERVAL 3 HOURS
            QUALIFY ROW_NUMBER() OVER (PARTITION BY o.station_id ORDER BY o.ts DESC) = 1

            UNION ALL

            -- Crescita sublineare del peso aggregato Netatmo: N sensori consumer
            -- indipendenti riducono la varianza come sqrt(N), non N. Dividere ogni
            -- peso per sqrt(N) impedisce che la mera densità di moduli (es. 130 a
            -- Firenze urbana) sommerga le stazioni SIR validate per conteggio.
            SELECT 'netatmo' AS src, ts, temp_c, humidity_pct, precip_mm,
                   wind_speed_ms, wind_dir_deg,
                   weight / sqrt(COUNT(*) OVER ()) AS w
            FROM netatmo_latest
        )
        SELECT
            -- ts_sir: MIN tra le stazioni SIR = dato più datato che contribuisce
            -- (freshness onesta). ts_netatmo: MAX (i moduli sono indipendenti).
            strftime(MIN(ts) FILTER (WHERE src = 'sir'),     '%Y-%m-%dT%H:%M:%SZ') AS ts_sir,
            strftime(MAX(ts) FILTER (WHERE src = 'netatmo'), '%Y-%m-%dT%H:%M:%SZ') AS ts_netatmo,
            strftime(MAX(ts), '%Y-%m-%dT%H:%M:%SZ')                                AS ts,
            ROUND(SUM(temp_c * w)       / NULLIF(SUM(CASE WHEN temp_c       IS NOT NULL THEN w ELSE 0 END), 0), 1) AS temp_c,
            ROUND(SUM(humidity_pct * w) / NULLIF(SUM(CASE WHEN humidity_pct IS NOT NULL THEN w ELSE 0 END), 0), 0) AS humidity_pct,
            ROUND(SUM(precip_mm * w)    / NULLIF(SUM(CASE WHEN precip_mm    IS NOT NULL THEN w ELSE 0 END), 0), 2) AS precip_mm,
            ROUND(SUM(wind_speed_ms * w)/ NULLIF(SUM(CASE WHEN wind_speed_ms IS NOT NULL THEN w ELSE 0 END), 0), 1) AS wind_speed_ms,
            ROUND(SUM(wind_dir_deg * w) / NULLIF(SUM(CASE WHEN wind_dir_deg IS NOT NULL THEN w ELSE 0 END), 0), 0) AS wind_dir_deg
        FROM obs
    """
    row = db.execute(blend_sql, [location_id, location_id]).fetchone()

    from_nwp = False
    if row is None or row[3] is None:
        # Fallback NWP: nessuna osservazione realtime utile. Media tra modelli
        # dell'ora forecast più vicina a now (ultimo run per sorgente).
        # ts_sir/ts_netatmo NULL: i dati non vengono da stazioni osservative.
        fallback_sql = """
            WITH f AS (
                SELECT temp_c, humidity_pct, precip_mm, wind_speed_ms, wind_dir_deg, ts_valid
                FROM forecasts
                WHERE location_id = ?
                  AND ts_valid >= NOW() - INTERVAL 2 HOURS
                  AND ts_valid <= NOW() + INTERVAL 2 HOURS
                  AND temp_c IS NOT NULL
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY source
                    ORDER BY ts_run DESC, abs(epoch(ts_valid) - epoch(NOW()))
                ) = 1
            )
            SELECT
                NULL                                          AS ts_sir,
                NULL                                          AS ts_netatmo,
                strftime(MAX(ts_valid), '%Y-%m-%dT%H:%M:%SZ') AS ts,
                ROUND(AVG(temp_c), 1)                        AS temp_c,
                ROUND(AVG(humidity_pct), 0)                  AS humidity_pct,
                ROUND(AVG(precip_mm), 2)                     AS precip_mm,
                ROUND(AVG(wind_speed_ms), 1)                 AS wind_speed_ms,
                ROUND(AVG(wind_dir_deg), 0)                  AS wind_dir_deg
            FROM f
        """
        row = db.execute(fallback_sql, [location_id]).fetchone()
        from_nwp = True

    if row is None or row[3] is None:
        return None

    ts_sir, ts_netatmo, ts, temp_c, humidity_pct, precip_mm, wind_speed_ms, wind_dir_deg = row
    if from_nwp:
        logger.debug(f"[{location_id}] current da fallback NWP (nessuna obs realtime)")

    # pressure_hpa e weather_code vengono dai forecast NWP (tabella forecasts),
    # non dalle stazioni realtime.
    pres_row = db.execute("""
        SELECT ROUND(AVG(pressure_hpa), 1)
        FROM forecasts
        WHERE location_id = ?
          AND ts_valid >= NOW() - INTERVAL 3 HOURS
          AND ts_valid <= NOW() + INTERVAL 1 HOUR
          AND pressure_hpa IS NOT NULL
    """, [location_id]).fetchone()
    pressure_hpa = pres_row[0] if pres_row else None

    # weather_code: moda dei codici WMO nell'ora più vicina a now tra tutti i modelli
    wc_rows = db.execute("""
        SELECT weather_code
        FROM forecasts
        WHERE location_id = ?
          AND ts_valid >= NOW() - INTERVAL 1 HOUR
          AND ts_valid <= NOW() + INTERVAL 1 HOUR
          AND weather_code IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY source, ts_valid ORDER BY ts_run DESC
        ) = 1
    """, [location_id]).fetchall()
    current_weather_code = _modal_weather_code(
        [int(r[0]) for r in wc_rows if r[0] is not None]
    )

    t    = float(temp_c)
    rh   = float(humidity_pct) if humidity_pct is not None else None
    ws  = float(wind_speed_ms) if wind_speed_ms is not None else None
    wd  = float(wind_dir_deg)  if wind_dir_deg  is not None else None

    dew      = _dewpoint(t, rh) if rh is not None else None
    apparent = _apparent_temp(t, rh, ws if ws is not None else 0.0) if rh is not None else None

    return {
        "ts":            ts,
        "ts_sir":        ts_sir,
        "ts_netatmo":    ts_netatmo,
        "temp_c":        t,
        "humidity_pct":  rh,
        "precip_mm":     float(precip_mm) if precip_mm is not None else None,
        "wind_speed_ms": ws,
        "wind_dir_deg":  wd,
        "dewpoint_c":    dew,
        "feels_like_c":  apparent,
        "pressure_hpa":  float(pressure_hpa) if pressure_hpa is not None else None,
        "weather_code":  current_weather_code,
    }


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
            ROUND(wind_speed_ms, 1)                      AS wind_speed_ms,
            weather_code
        FROM forecasts
        WHERE location_id = ?
          -- Margine 3h sotto mezzanotte UTC per coprire l'inizio del giorno locale
          -- (Europe/Rome): le ore 00-01 locali sono le 22-23Z del giorno prima.
          AND ts_valid >= CURRENT_DATE - INTERVAL 3 HOUR
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
        wc = r.get("weather_code")
        by_source.setdefault(src, []).append({
            "ts":            str(r["ts"]),
            "temp_c":        float(r["temp_c"]) if r["temp_c"] is not None else None,
            "humidity_pct":  float(r["humidity_pct"]) if r["humidity_pct"] is not None else None,
            "precip_mm":     float(r["precip_mm"]) if r["precip_mm"] is not None else None,
            "wind_speed_ms": float(r["wind_speed_ms"]) if r["wind_speed_ms"] is not None else None,
            "weather_code":  int(wc) if wc is not None and not pd.isna(wc) else None,
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
    precip_anchor: float | None,
    tmin_ci80_lo: float | None = None,
    tmin_ci80_hi: float | None = None,
    tmax_ci80_lo: float | None = None,
    tmax_ci80_hi: float | None = None,
    precip_ci80_lo: float | None = None,
    precip_ci80_hi: float | None = None,
) -> list[dict[str, float | None]] | None:
    """Profilo orario disaggregato da NWP ensemble, ancorato alle previsioni ML.

    Temperatura: rescaling lineare del profilo ensemble-mean da [raw_min, raw_max]
    a [tmin_p50, tmax_p50]. Se tmin_p50/tmax_p50 sono None usa i valori raw.

    Bande CI 80% orarie (opzionali): due ulteriori rescaling con gli stessi bound
    NWP ma ancorati a (tmin_ci80_lo, tmax_ci80_lo) e (tmin_ci80_hi, tmax_ci80_hi).
    Servono al frontend per disegnare la fascia di incertezza giornaliera.

    Precipitazione: distribuzione oraria NWP scalata proporzionalmente così che la
    somma giornaliera corrisponda a precip_anchor (E[precip] ML). precip_prob =
    frazione modelli con precip > 0.1mm/h per quell'ora. Le bande precip_orarie
    seguono la stessa shape ma con scale diverse (precip_ci80_lo/hi come
    anchor al posto di precip_anchor).

    Returns:
        Lista di 24 dict {hour, temp_c, temp_ci80_lo, temp_ci80_hi, humidity_pct,
        precip_mm, precip_ci80_lo, precip_ci80_hi, precip_prob, wind_speed_ms,
        weather_code} oppure None se non ci sono dati NWP per il giorno richiesto.
    """
    df = db.execute("""
        SELECT
            HOUR(local_ts)                                                      AS hour,
            AVG(temp_c)                                                         AS temp_mean,
            AVG(humidity_pct)                                                   AS humidity_mean,
            AVG(COALESCE(precip_mm, 0.0))                                       AS precip_mean,
            AVG(CASE WHEN precip_mm IS NULL THEN NULL
                     WHEN precip_mm > 0.1   THEN 1.0
                     ELSE 0.0 END)                                              AS precip_prob,
            AVG(wind_speed_ms)                                                  AS wind_mean
        FROM (
            SELECT
                ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS local_ts,
                temp_c, humidity_pct, precip_mm, wind_speed_ms
            FROM forecasts
            WHERE location_id = ?
              AND CAST(ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE) = ?
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

    # weather_code per ora: moda tra modelli (ogni (source, ts_valid) → run più recente)
    wc_df = db.execute("""
        SELECT HOUR(local_ts) AS hour, weather_code
        FROM (
            SELECT
                ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS local_ts,
                weather_code
            FROM forecasts
            WHERE location_id = ?
              AND CAST(ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE) = ?
              AND weather_code IS NOT NULL
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY source, ts_valid ORDER BY ts_run DESC
            ) = 1
        ) latest
    """, [location_id, target_date]).fetchall()

    # Raggruppa i codici WMO per ora e calcola la moda in Python (più semplice che in SQL)
    hour_wc_codes: dict[int, list[int]] = {}
    for wc_row in wc_df:
        h_val, wc_val = int(wc_row[0]), int(wc_row[1])
        hour_wc_codes.setdefault(h_val, []).append(wc_val)
    hour_wc_modal: dict[int, int | None] = {
        h: _modal_weather_code(codes) for h, codes in hour_wc_codes.items()
    }

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

    def _rescale_temp(v: float, lo: float | None, hi: float | None) -> float:
        if lo is None or hi is None:
            return round(v, 1)
        span_raw = raw_max - raw_min
        if span_raw <= 0:
            return round((lo + hi) / 2.0, 1)
        return round(lo + (v - raw_min) / span_raw * (hi - lo), 1)

    def _rescale_temp_p50(v: float) -> float:
        return _rescale_temp(v, tmin_p50, tmax_p50)

    total_precip_raw = sum(v for _, (_, _, v, _, _) in hour_data.items())
    if total_precip_raw > 0 and precip_anchor is not None and precip_anchor > 0:
        precip_scale = precip_anchor / total_precip_raw
    else:
        precip_scale = 0.0

    # Bande CI 80% precip: stesse proporzioni del rescale, ma ancorate ai bound CI.
    # Se uno dei bound manca o total_precip_raw = 0, la banda corrispondente è 0.
    precip_scale_lo = (
        (precip_ci80_lo / total_precip_raw)
        if total_precip_raw > 0 and precip_ci80_lo is not None and precip_ci80_lo > 0
        else 0.0
    )
    precip_scale_hi = (
        (precip_ci80_hi / total_precip_raw)
        if total_precip_raw > 0 and precip_ci80_hi is not None
        else 0.0
    )

    result: list[dict[str, float | int | None]] = []
    has_temp_band = tmin_ci80_lo is not None and tmax_ci80_lo is not None and tmin_ci80_hi is not None and tmax_ci80_hi is not None
    for h in range(24):
        if h in hour_data:
            t_raw, hum, p_raw, prob, wind = hour_data[h]
            result.append({
                "hour":          h,
                "temp_c":        _rescale_temp_p50(t_raw),
                "temp_ci80_lo":  _rescale_temp(t_raw, tmin_ci80_lo, tmax_ci80_lo) if has_temp_band else None,
                "temp_ci80_hi":  _rescale_temp(t_raw, tmin_ci80_hi, tmax_ci80_hi) if has_temp_band else None,
                "humidity_pct":  round(hum, 0) if hum is not None else None,
                "precip_mm":     round(p_raw * precip_scale, 2),
                "precip_ci80_lo": round(p_raw * precip_scale_lo, 2) if precip_scale_lo > 0 else None,
                "precip_ci80_hi": round(p_raw * precip_scale_hi, 2) if precip_scale_hi > 0 else None,
                "precip_prob":   round(prob, 2) if prob is not None else None,
                "wind_speed_ms": round(wind, 1) if wind is not None else None,
                "weather_code":  hour_wc_modal.get(h),
            })
        else:
            result.append({
                "hour": h, "temp_c": None, "temp_ci80_lo": None, "temp_ci80_hi": None,
                "humidity_pct": None,
                "precip_mm": None, "precip_ci80_lo": None, "precip_ci80_hi": None,
                "precip_prob": None, "wind_speed_ms": None,
                "weather_code": None,
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
        db:   se fornito, aggiunge current, air_quality, nwp_models_hourly al payload

    Struttura JSON:
      {location_id, generated_at, coverage_empirical_30d,
       current?, air_quality?, nwp_models_hourly?,
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
        ev = expected_precip(t)
        return {
            "mean":    round(ev, 2) if ev is not None else None,
            "p50":     _c(t.get("p50")),
            "ci80_lo": _c(t.get("ci80_lo")), "ci80_hi": _c(t.get("ci80_hi")),
            "ci90_lo": _c(t.get("ci90_lo")), "ci90_hi": _c(t.get("ci90_hi")),
        }

    day_payloads = []
    for day in days:
        pred: dict[str, dict[str, float]] = day["pred"]
        inds: list[IndicatorResult] = day["indicators"]
        day_payloads.append({
            "target_date":  day["target_date"],
            "lead_time_h":  day["lead_time_h"],
            "weather_code": day.get("weather_code"),
            "forecasts": {
                "tmin_c":    _fmt_target(pred.get("tmin_c",    {})),
                "tmax_c":    _fmt_target(pred.get("tmax_c",    {})),
                "precip_mm": _fmt_precip(pred.get("precip_mm", {})),
            },
            "indicators": {
                r.indicator_id: {
                    "verdict": r.verdict,
                    "rule_matched": r.rule_matched,
                    "rule_text": r.rule_text,
                }
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
        payload["nwp_models_hourly"]  = get_nwp_models_hourly(db, location_id)
        payload["air_quality"]        = get_current_air_quality(db, location_id)
    payload["days"] = day_payloads

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{location_id}.json"
    # Scrittura atomica: nginx serve questi file mentre il cron li riscrive —
    # un write_text diretto esporrebbe un JSON troncato ai client.
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp_path.replace(path)
    return path
