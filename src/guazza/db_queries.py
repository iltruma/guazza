"""Query SQL a DuckDB per condizioni meteo attuali e storiche.

Queste funzioni leggono da forecasts, observations e predictions in DuckDB
e restituiscono dati formattati per il frontend (JSON) o per il job di forecast.
Non scrivono nel database.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger

if TYPE_CHECKING:
    from guazza.storage import DuckDBClient


# Ordine e label per il confronto modelli nel frontend
_MODEL_ORDER = [
    "open_meteo_ecmwf_ifs",
    "open_meteo_icon_eu",
    "open_meteo_arome_france",
    "open_meteo_italia_meteo_arpae_icon_2i",
]
_MODEL_LABELS: dict[str, str] = {
    "open_meteo_ecmwf_ifs":                  "ECMWF IFS",
    "open_meteo_icon_eu":                    "ICON-EU",
    "open_meteo_arome_france":               "AROME",
    "open_meteo_italia_meteo_arpae_icon_2i": "ICON-2I",
}

# Severità WMO per il tie-break quando più codici hanno la stessa frequenza modale.
# Valori più alti = condizione più severa.
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

_MIN_HOURS_FOR_DAILY_CODE = 20
_MIN_CODE_HOURS_FOR_SEVERITY = 2


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


def _modal_weather_code(codes: list[int]) -> int | None:
    """Codice WMO modale. In caso di pareggio, vince il codice più severo."""
    if not codes:
        return None
    counter = Counter(codes)
    max_freq = max(counter.values())
    candidates = [c for c, f in counter.items() if f == max_freq]
    return max(candidates, key=lambda c: _WMO_SEVERITY.get(c, 0))


def _pessimistic_weather_code(codes: list[int]) -> int | None:
    """Codice WMO pessimistico: il più severo che appare in almeno _MIN_CODE_HOURS_FOR_SEVERITY ore."""
    if not codes:
        return None
    counter = Counter(codes)
    stable = [c for c, n in counter.items() if n >= _MIN_CODE_HOURS_FOR_SEVERITY]
    candidates = stable if stable else list(counter.keys())
    return max(candidates, key=lambda c: _WMO_SEVERITY.get(c, 0))


def _dewpoint(t: float, rh: float) -> float:
    """Punto di rugiada (°C) via formula di Magnus."""
    a, b = 17.625, 243.04
    gamma = math.log(rh / 100.0) + a * t / (b + t)
    return round(b * gamma / (a - gamma), 1)


def _apparent_temp(t: float, rh: float, ws: float) -> float:
    """Temperatura apparente Steadman/BoM (°C). ws in m/s."""
    e = (rh / 100.0) * 6.105 * math.exp(17.27 * t / (237.7 + t))
    return round(t + 0.33 * e - 0.70 * ws - 4.00, 1)


# ── SQL templates per get_current_conditions ──────────────────────────────────

_BLEND_SQL = """
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
        -- indipendenti riducono la varianza come sqrt(N), non N.
        SELECT 'netatmo' AS src, ts, temp_c, humidity_pct, precip_mm,
               wind_speed_ms, wind_dir_deg,
               weight / sqrt(COUNT(*) OVER ()) AS w
        FROM netatmo_latest
    )
    SELECT
        -- ts_sir: MIN = dato più datato che contribuisce (freshness onesta).
        -- ts_netatmo: MAX (i moduli sono indipendenti).
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

_FALLBACK_SQL = """
    WITH f AS (
        SELECT temp_c, humidity_pct, precip_mm, wind_speed_ms, wind_dir_deg, ts_valid
        FROM forecasts
        WHERE location_id = ?
          AND ts_valid >= NOW() - INTERVAL 2 HOURS
          AND ts_valid <= NOW() + INTERVAL 2 HOURS
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


def _query_recent_obs(
    db: DuckDBClient,
    location_id: str,
) -> dict[str, Any] | None:
    """Blend pesato delle osservazioni realtime (SIR + Netatmo, ultimi 3h).

    Returns None se non ci sono osservazioni utili
    (temp/humidity/precip/wind tutti null).
    """
    row = db.execute(_BLEND_SQL, [location_id, location_id]).fetchone()
    if row is None or not any(v is not None for v in row[3:7]):
        return None
    return {
        "ts_sir": row[0], "ts_netatmo": row[1], "ts": row[2],
        "temp_c": row[3], "humidity_pct": row[4],
        "precip_mm": row[5], "wind_speed_ms": row[6], "wind_dir_deg": row[7],
    }


def _query_nwp_fallback(
    db: DuckDBClient,
    location_id: str,
) -> dict[str, Any] | None:
    """Media NWP dell'ora più vicina a now (ultimo run per sorgente).

    Returns None se non ci sono forecast disponibili. temp_c può essere None
    (usato per il fill per-variabile; il controllo temp_c is None compete al chiamante).
    """
    nwp = db.execute(_FALLBACK_SQL, [location_id]).fetchone()
    if nwp is None:
        return None
    return {
        "ts": nwp[2],
        "temp_c": nwp[3], "humidity_pct": nwp[4],
        "precip_mm": nwp[5], "wind_speed_ms": nwp[6], "wind_dir_deg": nwp[7],
    }


def get_current_conditions(
    db: DuckDBClient,
    location_id: str,
) -> dict[str, Any] | None:
    """Condizioni attuali: blend stazioni realtime (SIR + Netatmo) con fallback NWP.

    Legge il blend delle stazioni degli ultimi 3h. Se alcune variabili mancano
    (es. anemometro assente), le riempie variabile per variabile con la media NWP
    dell'ora più vicina. Se non ci sono osservazioni, usa tutto il NWP.
    Se non ci sono nemmeno forecast NWP, ritorna None.

    `sources` dichiara la provenienza per-variabile: "realtime" (blend stazioni),
    "nwp" (fallback), o None (assente).
    """
    obs = _query_recent_obs(db, location_id)

    # Carica il NWP se: non ci sono obs, oppure alcune variabili del blend sono null
    need_nwp = obs is None or any(
        obs[k] is None
        for k in ("temp_c", "humidity_pct", "precip_mm", "wind_speed_ms", "wind_dir_deg")
    )
    nwp = _query_nwp_fallback(db, location_id) if need_nwp else None

    if obs is None:
        if nwp is None or nwp["temp_c"] is None:
            return None
        ts_sir = ts_netatmo = None
        ts = nwp["ts"]
        b_temp = b_hum = b_precip = b_wind = b_wdir = None
        temp_c       = nwp["temp_c"]
        humidity_pct = nwp["humidity_pct"]
        precip_mm    = nwp["precip_mm"]
        wind_speed_ms = nwp["wind_speed_ms"]
        wind_dir_deg  = nwp["wind_dir_deg"]
        logger.debug(f"[{location_id}] current da fallback NWP (nessuna obs realtime)")
    else:
        ts_sir    = obs["ts_sir"]
        ts_netatmo = obs["ts_netatmo"]
        ts        = obs["ts"]
        b_temp, b_hum, b_precip, b_wind, b_wdir = (
            obs["temp_c"], obs["humidity_pct"], obs["precip_mm"],
            obs["wind_speed_ms"], obs["wind_dir_deg"],
        )

        def _fill(b_val: Any, nwp_key: str) -> Any:
            """Valore dal blend stazioni, o dal NWP se la variabile è mancante."""
            if b_val is not None:
                return b_val
            return nwp[nwp_key] if nwp is not None else None

        temp_c       = _fill(b_temp,  "temp_c")
        humidity_pct = _fill(b_hum,   "humidity_pct")
        precip_mm    = _fill(b_precip, "precip_mm")
        wind_speed_ms = _fill(b_wind,  "wind_speed_ms")
        wind_dir_deg  = _fill(b_wdir,  "wind_dir_deg")

    if temp_c is None:
        return None

    def _source(b_val: Any, final_val: Any) -> str | None:
        """Provenance di una variabile: blend stazioni o NWP."""
        if final_val is None:
            return None
        return "realtime" if b_val is not None else "nwp"

    sources: dict[str, str | None] = {
        "temp_c":        _source(b_temp,   temp_c),
        "humidity_pct":  _source(b_hum,    humidity_pct),
        "precip_mm":     _source(b_precip, precip_mm),
        "wind_speed_ms": _source(b_wind,   wind_speed_ms),
        "wind_dir_deg":  _source(b_wdir,   wind_dir_deg),
    }
    wind_speed_source = sources["wind_speed_ms"]

    if wind_speed_source == "nwp" and obs is not None:
        logger.debug(f"[{location_id}] vento da fallback NWP (obs realtime senza anemometro)")

    # pressure_hpa e weather_code vengono sempre dai forecast NWP
    pres_row = db.execute("""
        SELECT ROUND(AVG(pressure_hpa), 1)
        FROM forecasts
        WHERE location_id = ?
          AND ts_valid >= NOW() - INTERVAL 3 HOURS
          AND ts_valid <= NOW() + INTERVAL 1 HOUR
          AND pressure_hpa IS NOT NULL
    """, [location_id]).fetchone()
    pressure_hpa = pres_row[0] if pres_row else None

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

    sources["pressure_hpa"] = "nwp" if pressure_hpa is not None else None
    sources["weather_code"] = "nwp" if current_weather_code is not None else None

    t  = float(temp_c)
    rh = float(humidity_pct) if humidity_pct is not None else None
    ws = float(wind_speed_ms) if wind_speed_ms is not None else None
    wd = float(wind_dir_deg)  if wind_dir_deg  is not None else None

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
        "wind_speed_source": wind_speed_source,
        "dewpoint_c":    dew,
        "feels_like_c":  apparent,
        "pressure_hpa":  float(pressure_hpa) if pressure_hpa is not None else None,
        "weather_code":  current_weather_code,
        "sources":       sources,
    }


def get_daily_weather_code(
    db: DuckDBClient,
    location_id: str,
    target_date: str,
) -> int | None:
    """Codice WMO giornaliero: caso pessimistico tra modelli NWP.

    Per ogni (source, ts_valid) prende il run più recente, esclude le source
    con dati parziali (< _MIN_HOURS_FOR_DAILY_CODE ore), poi ritorna il codice
    di severità massima che appare in almeno _MIN_CODE_HOURS_FOR_SEVERITY ore.

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
