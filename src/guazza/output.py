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
   Real-time obs   — level_sir
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger

from guazza.db_queries import (
    _get,
    _modal_weather_code,
    get_current_conditions,
    get_nwp_models_hourly,
)

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


SignalBag = dict[str, float | None]

_NWP_WIND_COLS = [
    "ecmwf_wind_ms", "icon_wind_ms",
    "arome_wind_ms", "icon2i_wind_ms",
]
_NWP_HUM_COLS = [
    "ecmwf_humidity_pct", "icon_humidity_pct",
    "arome_humidity_pct", "icon2i_humidity_pct",
]

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
        obs_summary: valori real-time: level_sir (opzionale)
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

    prob_rain = pred.get("rain_clf", {}).get("prob_rain")
    return {
        # Precipitazione (ML quantile → CDF inversa lineare, o classificatore diretto)
        "P(precip > 0.2mm)": prob_rain if prob_rain is not None else _prob_exceeds(precip_q, 0.2),
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
    has_temp_band = (
        tmin_ci80_lo is not None and tmax_ci80_lo is not None
        and tmin_ci80_hi is not None and tmax_ci80_hi is not None
    )
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
        db:   se fornito, aggiunge current, nwp_models_hourly al payload

    Struttura JSON:
      {location_id, generated_at, updates, coverage_empirical_30d,
       current?, nwp_models_hourly?,
       days: [{target_date, lead_time_h, forecasts, indicators, hourly}, ...]}

    `updates.pipeline_at` è lo stesso timestamp di `generated_at`; se il JSON
    esiste già viene preservato `updates.realtime_at` del payload precedente
    (il patch realtime più recente sopravvive alla riscrittura della pipeline).

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
                "precip_mm": {
                    **_fmt_precip(pred.get("precip_mm", {})),
                    "prob_rain": pred.get("rain_clf", {}).get("prob_rain"),
                },
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

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{location_id}.json"
    # Se il JSON esiste già, preserva updates.realtime_at dal payload precedente:
    # il patch realtime più recente sopravvive alla riscrittura della pipeline.
    realtime_at: str | None = None
    if path.exists():
        try:
            realtime_at = (json.loads(path.read_text()).get("updates") or {}).get("realtime_at")
        except (json.JSONDecodeError, OSError):
            logger.warning(f"[{location_id}] JSON esistente non leggibile — updates.realtime_at non preservato")

    now_iso = datetime.now(tz=UTC).isoformat()
    payload: dict[str, Any] = {
        "location_id":            location_id,
        "generated_at":           now_iso,
        "updates":                {"pipeline_at": now_iso, "realtime_at": realtime_at},
        "coverage_empirical_30d": coverage,
    }
    if db is not None:
        payload["current"]            = get_current_conditions(db, location_id)
        payload["nwp_models_hourly"]  = get_nwp_models_hourly(db, location_id)
    payload["days"] = day_payloads

    # Scrittura atomica: nginx serve questi file mentre il cron li riscrive —
    # un write_text diretto esporrebbe un JSON troncato ai client.
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp_path.replace(path)
    return path


# Suffisso temp distinto da write_location_json (.json.tmp): il refresh realtime
# può girare mentre la pipeline 6h scrive il proprio tmp — due processi sullo
# stesso path si clobbererebbero il file a metà scrittura.
_REALTIME_TMP_SUFFIX = ".realtime.tmp"


def refresh_realtime_json(
    db: DuckDBClient,
    location_id: str,
    output_dir: Path,
) -> Path | None:
    """Aggiorna `current` nel JSON esistente di una location.

    Chiamato da `guazza-ingest realtime` dopo le scritture in DuckDB: ricalcola
    le condizioni attuali con `get_current_conditions` (la stessa funzione usata
    dalla pipeline) e sostituisce il solo campo top-level `current` del JSON già
    prodotto, impostando `updates.realtime_at` al completamento del patch
    (preservando `updates.pipeline_at`). Tutti gli altri campi — location_id,
    generated_at, coverage_empirical_30d, days, nwp_models_hourly — restano
    intatti: nessun ricalcolo di forecast/features/predict.

    Se il JSON non esiste ancora fa skip senza crearlo: lo genererà la prima
    pipeline (la tabella predictions non è ancora backfillata).

    Scrittura atomica (temp file + os.replace) con cleanup del temp anche in
    caso di errore: nginx serve questi file mentre il cron li riscrive.

    Returns:
        Path del file aggiornato, oppure None se il JSON non esiste.
    """
    path = output_dir / f"{location_id}.json"
    if not path.exists():
        logger.info(f"[{location_id}] JSON non ancora generato dalla pipeline — skip refresh realtime")
        return None

    current = get_current_conditions(db, location_id)

    payload = json.loads(path.read_text())
    payload["current"] = current

    # Metadata temporale: pipeline_at resta quello scritto dalla pipeline,
    # realtime_at è il completamento di questo patch. JSON legacy senza
    # `updates` viene normalizzato a struttura valida (pipeline_at null).
    prev_updates = payload.get("updates")
    payload["updates"] = {
        "pipeline_at": prev_updates.get("pipeline_at") if isinstance(prev_updates, dict) else None,
        "realtime_at": datetime.now(tz=UTC).isoformat(),
    }

    tmp_path = path.with_name(path.name + _REALTIME_TMP_SUFFIX)
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return path
