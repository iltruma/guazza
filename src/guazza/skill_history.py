"""Skill history: append giornaliero e dump JSON per il frontend.

Funzioni pure usate da jobs/review.py (ogni 6h) per backfill e dump giornaliero.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from datetime import date as date_type
from pathlib import Path

import duckdb
from loguru import logger

from guazza.features import NWP_MODEL_PREFIXES

LEAD_H = 24
VARS = ["tmin_c", "tmax_c", "precip_mm"]
NWP_SOURCES = [f"open_meteo_{src}" for _prefix, src in NWP_MODEL_PREFIXES]
ALL_SOURCES = ["guazza", *NWP_SOURCES]
DEFAULT_DUMP_PATH = Path("frontend/data/skill_history.json")


def _sql_guazza_forecast(target_date: date_type) -> str:
    return f"""
        SELECT location_id, tmin_p50 AS tmin_c, tmax_p50 AS tmax_c, precip_p50 AS precip_mm
        FROM predictions
        WHERE ts_valid = '{target_date}'
          AND lead_time_h BETWEEN 23 AND 25
    """


def _sql_nwp_forecast(target_date: date_type) -> str:
    return f"""
        SELECT
            source, location_id,
            MIN(temp_c)    AS tmin_c,
            MAX(temp_c)    AS tmax_c,
            SUM(precip_mm) AS precip_mm
        FROM (
            SELECT source, location_id, temp_c, precip_mm
            FROM forecasts
            WHERE ts_valid::DATE = '{target_date}'
              AND EXTRACT(EPOCH FROM (ts_valid - ts_run)) / 3600.0
                  BETWEEN 23.0 AND 25.0
        )
        GROUP BY source, location_id
    """


def _sql_actual(target_date: date_type) -> str:
    return f"""
        SELECT location_id, tmin_c, tmax_c, precip_mm
        FROM obs_weighted_daily
        WHERE obs_date = '{target_date}'
    """


def _sql_upsert(rows: list[tuple]) -> str:
    if not rows:
        return ""
    values = ", ".join(
        f"('{loc}','{d}','{src}','{var}',{LEAD_H},"
        f"{f_val if f_val is not None else 'NULL'},"
        f"{a_val if a_val is not None else 'NULL'},"
        f"{e_val if e_val is not None else 'NULL'})"
        for (loc, d, src, var, f_val, a_val, e_val) in rows
    )
    return f"""
        INSERT INTO skill_history_daily
            (location_id, target_date, source, variable, lead_h,
             forecast_value, actual_value, abs_error)
        VALUES {values}
        ON CONFLICT (location_id, target_date, source, variable, lead_h) DO UPDATE SET
            forecast_value = EXCLUDED.forecast_value,
            actual_value   = EXCLUDED.actual_value,
            abs_error      = EXCLUDED.abs_error
    """


def _collect_rows(con: duckdb.DuckDBPyConnection, target_date: date_type) -> list[tuple]:
    actuals = {
        r[0]: {"tmin_c": r[1], "tmax_c": r[2], "precip_mm": r[3]}
        for r in con.execute(_sql_actual(target_date)).fetchall()
    }
    if not actuals:
        return []

    guazza = {
        r[0]: {"tmin_c": r[1], "tmax_c": r[2], "precip_mm": r[3]}
        for r in con.execute(_sql_guazza_forecast(target_date)).fetchall()
    }
    nwp = {
        (r[0], r[1]): {"tmin_c": r[2], "tmax_c": r[3], "precip_mm": r[4]}
        for r in con.execute(_sql_nwp_forecast(target_date)).fetchall()
    }

    rows: list[tuple] = []
    for loc, a in actuals.items():
        for var in VARS:
            actual = a.get(var)
            if actual is None:
                continue
            fc = (guazza.get(loc) or {}).get(var)
            if fc is not None:
                rows.append((loc, target_date, "guazza", var, fc, actual, abs(fc - actual)))
            for src in NWP_SOURCES:
                fc = nwp.get((src, loc), {}).get(var)
                if fc is None:
                    continue
                rows.append((loc, target_date, src, var, fc, actual, abs(fc - actual)))
    return rows


def append_one(con: duckdb.DuckDBPyConnection, target_date: date_type) -> int:
    """Upsert le righe per un singolo giorno. Ritorna il numero inserito."""
    rows = _collect_rows(con, target_date)
    if not rows:
        logger.info(f"skill_history: {target_date} — nessuna riga (no obs)")
        return 0
    con.execute(_sql_upsert(rows))
    return len(rows)


def dump_payload(con: duckdb.DuckDBPyConnection) -> dict:
    """Aggrega skill_history_daily in un JSON time series per il frontend."""
    rows = con.execute("""
        SELECT location_id, variable, target_date, source,
               forecast_value, actual_value
        FROM skill_history_daily
        WHERE lead_h = ?
        -- ORDER BY target_date è load-bearing: dump_payload si affida all'ordine
        -- crescente per costruire la lista dates in by_lv senza sort esplicito
        ORDER BY location_id, variable, target_date, source
    """, [LEAD_H]).fetchall()

    payload: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "lead_h": LEAD_H,
        "sources": ALL_SOURCES,
        "variables": VARS,
        "min_date": None,
        "max_date": None,
        "locations": {},
    }
    by_lv: dict[tuple[str, str], dict] = {}

    for loc, var, d, src, f_val, a_val in rows:
        key = (loc, var)
        if key not in by_lv:
            by_lv[key] = {"dates": [], "_seen_dates": set(), "_actual": {}, "_forecast": {}}
        entry = by_lv[key]
        if d not in entry["_seen_dates"]:
            entry["_seen_dates"].add(d)
            entry["dates"].append(d)
        entry["_actual"][d] = a_val
        entry["_forecast"].setdefault(src, {})[d] = f_val

    min_d, max_d = None, None
    for (loc, var), entry in by_lv.items():
        loc_dict = payload["locations"].setdefault(loc, {})
        var_dict: dict = {
            "dates": [d.isoformat() for d in entry["dates"]],
            "actual": [entry["_actual"].get(d) for d in entry["dates"]],
        }
        for src in ALL_SOURCES:
            var_dict[src] = [entry["_forecast"].get(src, {}).get(d) for d in entry["dates"]]
        loc_dict[var] = var_dict
        if entry["dates"]:
            dmin, dmax = min(entry["dates"]), max(entry["dates"])
            min_d = dmin if min_d is None or dmin < min_d else min_d
            max_d = dmax if max_d is None or dmax > max_d else max_d

    payload["min_date"] = min_d.isoformat() if min_d else None
    payload["max_date"] = max_d.isoformat() if max_d else None
    return payload


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(path)
