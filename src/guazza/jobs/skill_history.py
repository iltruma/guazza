"""Entry point cron — append giornaliero di skill history + dump JSON per il frontend.

Calcola, per ogni combinazione (location, target_date, source, variable, lead_h=24):
- `forecast_value`: previsione emessa a D-1 per D
- `actual_value`: osservazione SIR pesata a D
- `abs_error`: |forecast - actual|

Popola `skill_history_daily` (PK su tutti i campi, append idempotente).
Il comando `dump` aggrega la tabella in un JSON time series per il frontend
(`affidabilita.html`): per ogni location × variable una serie di date con
valori per ogni source, in modo che il frontend possa filtrare per finestra
(7gg / 30gg / totale).

Schedule proposta (k8s CronJob, in `k8s/apps/guazza/cronjob.yaml`):
  `15 6 * * *` UTC — 15 min dopo `daily` ingest (06:00), così le obs di ieri
  sono nel DB quando il job parte.

Read-only su `predictions` e `forecasts`; upsert in `skill_history_daily`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.features import NWP_MODEL_PREFIXES
from guazza.jobs._common import DB_OPTION, job_run
from guazza.storage import DuckDBClient

app = typer.Typer(help="Skill history append giornaliero + dump JSON per il frontend.")

# Costanti
LEAD_H = 24
VARS = ["tmin_c", "tmax_c", "precip_mm"]
# Nomi source in `forecasts` (DuckDB). Costruiti una volta.
NWP_SOURCES = [f"open_meteo_{src}" for _prefix, src in NWP_MODEL_PREFIXES]
ALL_SOURCES = ["guazza", *NWP_SOURCES]
DEFAULT_DUMP_PATH = Path("frontend/data/skill_history.json")


# ── helpers SQL ─────────────────────────────────────────────────────────────

def _sql_guazza_forecast(target_date: date) -> str:
    """Per ogni location: tmin_p50/tmax_p50/precip_p50 con lead ~24h.

    `lead_time_h BETWEEN 23 AND 25` copre ±1h la finestra del forecast di ieri
    per oggi, dato che i run nominali partono a intervalli discreti (6h ECMWF,
    3h ICON-D2) e talvolta slittano di un'ora.
    """
    return f"""
        SELECT location_id, tmin_p50 AS tmin_c, tmax_p50 AS tmax_c, precip_p50 AS precip_mm
        FROM predictions
        WHERE ts_valid = '{target_date}'
          AND lead_time_h BETWEEN 23 AND 25
    """


def _sql_nwp_forecast(target_date: date) -> str:
    """Per ogni (location, source): tmin/tmax/precip daily aggregati dai record
    orari con lead ~24h. SUM/MAX/MIN come in `features.daily_nwp` (CTE 3).
    """
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


def _sql_actual(target_date: date) -> str:
    """Per ogni location: tmin_c, tmax_c, precip_mm da obs_weighted_daily."""
    return f"""
        SELECT location_id, tmin_c, tmax_c, precip_mm
        FROM obs_weighted_daily
        WHERE obs_date = '{target_date}'
    """


def _sql_upsert(rows: list[tuple]) -> str:
    """Costruisce un VALUES + INSERT ... ON CONFLICT DO UPDATE."""
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


# ── append ──────────────────────────────────────────────────────────────────

def _collect_rows(
    con: duckdb.DuckDBPyConnection, target_date: date
) -> list[tuple]:
    """Ritorna la lista di tuple pronte per l'upsert."""
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
            # Guazza
            fc = (guazza.get(loc) or {}).get(var)
            if fc is not None:
                err = abs(fc - actual)
                rows.append((loc, target_date, "guazza", var, fc, actual, err))
            # NWP
            for src in NWP_SOURCES:
                fc = nwp.get((src, loc), {}).get(var)
                if fc is None:
                    continue
                err = abs(fc - actual)
                rows.append((loc, target_date, src, var, fc, actual, err))
    return rows


def _append_one(
    con: duckdb.DuckDBPyConnection, target_date: date
) -> int:
    """Upsert le righe per un singolo giorno. Ritorna il numero inserito."""
    rows = _collect_rows(con, target_date)
    if not rows:
        logger.info(f"skill_history: {target_date} — nessuna riga (no obs)")
        return 0
    con.execute(_sql_upsert(rows))
    return len(rows)


# ── dump ────────────────────────────────────────────────────────────────────

def _dump_payload(con: duckdb.DuckDBPyConnection) -> dict:
    """Aggrega skill_history_daily in un JSON time series per il frontend."""
    rows = con.execute("""
        SELECT location_id, variable, target_date, source,
               forecast_value, actual_value
        FROM skill_history_daily
        WHERE lead_h = ?
        ORDER BY location_id, variable, target_date, source
    """, [LEAD_H]).fetchall()

    # Struttura: {location: {variable: {dates, actual, source1, source2, ...}}}
    payload: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "lead_h": LEAD_H,
        "sources": ALL_SOURCES,
        "variables": VARS,
        "min_date": None,
        "max_date": None,
        "locations": {},
    }
    # Accumulo: per (loc, var) → dict per source, con liste di (date, value)
    by_lv: dict[tuple[str, str], dict] = {}

    for loc, var, d, src, f_val, a_val in rows:
        key = (loc, var)
        if key not in by_lv:
            by_lv[key] = {"dates": [], "_seen_dates": set(),
                          "_actual": {}, "_forecast": {}}
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
        for src in NWP_SOURCES + ["guazza"]:
            var_dict[src] = [entry["_forecast"].get(src, {}).get(d)
                              for d in entry["dates"]]
        loc_dict[var] = var_dict
        if entry["dates"]:
            dmin, dmax = min(entry["dates"]), max(entry["dates"])
            min_d = dmin if min_d is None or dmin < min_d else min_d
            max_d = dmax if max_d is None or dmax > max_d else max_d

    payload["min_date"] = min_d.isoformat() if min_d else None
    payload["max_date"] = max_d.isoformat() if max_d else None
    return payload


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(path)


# ── commands ────────────────────────────────────────────────────────────────

@app.command()
def append(
    db: Path = DB_OPTION,
    day: str = typer.Option(
        None,
        help="YYYY-MM-DD; default = ieri. Accetta anche --days N per backfill.",
    ),
    days: int = typer.Option(
        1, help="Numero di giorni all'indietro da processare (default 1 = solo ieri)"
    ),
) -> None:
    """Calcola forecast (lead 24h) vs actual per ogni location/source/variable
    e fa upsert in `skill_history_daily`. Idempotente.
    """
    setup_logging()
    with job_run("job_skill_history_append") as stats:
        with DuckDBClient(db_path=db) as client:
            # init_schema è idempotente (IF NOT EXISTS / _ensure_* helpers).
            client.init_schema()
            assert client._conn is not None
            con = client._conn
            if day is not None:
                # --day ha priorità: processa solo quel giorno
                target = date.fromisoformat(day)
                days = 1
            else:
                target = date.today() - timedelta(days=1)

            total_rows = 0
            for offset in range(days):
                d = target - timedelta(days=offset)
                n = _append_one(con, d)
                logger.info(f"skill_history: {d} → {n} righe upsert")
                total_rows += n
            logger.info(
                f"skill_history: totali {total_rows} righe su {days} giorno/i"
            )
            stats.rows = total_rows
            stats.summary = f"append: {total_rows} righe"


@app.command()
def dump(
    db: Path = DB_OPTION,
    output: Path = typer.Option(
        DEFAULT_DUMP_PATH, "--output", help="Path JSON di output"
    ),
) -> None:
    """Aggrega `skill_history_daily` in un JSON time series per il frontend."""
    setup_logging()
    with job_run("job_skill_history_dump") as stats:
        con = duckdb.connect(str(db), read_only=True)
        try:
            payload = _dump_payload(con)
        finally:
            con.close()

        _atomic_write_json(output, payload)
        n_loc = len(payload["locations"])
        n_dates = (
            len(next(iter(next(iter(payload["locations"].values())).values()))["dates"])
            if n_loc else 0
        )
        logger.info(
            f"skill_history.json: {n_loc} location, finestra "
            f"{payload['min_date']}→{payload['max_date']}"
        )
        stats.rows = n_loc
        stats.summary = f"dump: {n_loc} location × {n_dates} date"


if __name__ == "__main__":
    app()
