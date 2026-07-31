"""Entry point cron — curva di skill per-location MAE Guazza vs NWP per lead.

Quantifica, in modo onesto e retrospettivo, quanto il post-processing batte il NWP
grezzo a ogni orizzonte D+0…D+7. La verità è la stazione SIR primaria di ciascuna
location (gauge indipendente, NON il target pesato su cui il modello è addestrato).

Metodologia (identica a `analysis/backtest_multilead.py`, qui resa job idempotente):
  1. Train globale q=0.50 (location-id categorica) sui dati ≤ cutoff (= window_start − embargo).
  2. Predici out-of-sample sulla finestra multi-lead (lead lunghi da `ingest multilead`).
  3. Per (location, variabile, lead): MAE Guazza e MAE NWP vs gauge primario, skill%.
  4. Scrivi `{output_dir}/skill.json` (una curva per location).

Finestra limitata dall'archivio `previous_dayN` (~ott 2025 → oggi): è una finestra di
mesi, non lifetime — il frontend lo dichiara esplicitamente.

Cron: settimanale, dopo ingest multilead + train run. Read-only sul DB.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd
import typer
import yaml
from loguru import logger

from guazza._logging import setup_logging
from guazza.jobs._common import CONFIG_DIR_OPTION, DB_OPTION, OUTPUT_DIR_OPTION, job_run
from guazza.models import _TARGET_NWP_MEAN, FEATURE_COLS, _train_lgbm

app = typer.Typer(help="Curva di skill Guazza vs NWP per lead.")

# Variabili in curva: la precip ha MAE troppo rumorosa per una curva pulita.
SKILL_VARS = ["tmin_c", "tmax_c"]
LEADS = [0, 24, 48, 72, 96, 120, 144, 168]
# Sotto questa soglia il punto è statisticamente insignificante → null nel JSON.
MIN_SAMPLES_PER_LEAD = 5


@app.callback()
def _callback() -> None:
    setup_logging()


def _primary_stations(config_dir: Path) -> dict[str, str]:
    data = yaml.safe_load((config_dir / "locations.yaml").read_text())
    return {loc_id: spec["sir_station_id"]
            for loc_id, spec in data["locations"].items()
            if spec.get("sir_station_id")}


def _load_features(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute("SELECT * FROM features_daily").df()
    df["location_id"] = df["location_id"].astype("category")
    df["target_date"] = pd.to_datetime(df["target_date"]).dt.date
    return df.sort_values("target_date").reset_index(drop=True)


def _load_primary_obs(con: duckdb.DuckDBPyConnection, stations: dict[str, str]) -> pd.DataFrame:
    values = ", ".join(f"('{loc}','{st}')" for loc, st in stations.items())
    df = con.execute(f"""
        WITH st(location_id, station_id) AS (VALUES {values})
        SELECT st.location_id, o.ts::date AS target_date,
               o.tmin_c AS prim_tmin_c, o.tmax_c AS prim_tmax_c
        FROM observations o JOIN st ON o.station_id = st.station_id
        WHERE o.source = 'sir_toscana' AND o.granularity = 'daily'
    """).df()
    df["target_date"] = pd.to_datetime(df["target_date"]).dt.date
    return df


def _curve_for(test: pd.DataFrame, var: str) -> list[dict[str, object]]:
    nwp_col = _TARGET_NWP_MEAN[var]
    prim_col = f"prim_{var.replace('_c', '')}_c"  # prim_tmin_c / prim_tmax_c
    points: list[dict[str, object]] = []
    for lead in LEADS:
        g = test[test["lead_time_h"] == lead].dropna(
            subset=[f"pred_{var}", nwp_col, prim_col]
        )
        n = len(g)
        if n < MIN_SAMPLES_PER_LEAD:
            points.append({"lead_h": lead, "n": n,
                           "mae_nwp": None, "mae_ml": None, "skill_pct": None})
            continue
        mae_nwp = float((g[nwp_col] - g[prim_col]).abs().mean())
        mae_ml = float((g[f"pred_{var}"] - g[prim_col]).abs().mean())
        skill = (1 - mae_ml / mae_nwp) * 100 if mae_nwp else None
        points.append({
            "lead_h": lead, "n": n,
            "mae_nwp": round(mae_nwp, 3), "mae_ml": round(mae_ml, 3),
            "skill_pct": round(skill, 1) if skill is not None else None,
        })
    return points


@app.command()
def run(
    db: Path = DB_OPTION,
    output_dir: Path = OUTPUT_DIR_OPTION,
    config_dir: Path = CONFIG_DIR_OPTION,
    window_start: str = typer.Option("2025-10-15", help="Inizio finestra multi-lead (test)"),
    embargo_days: int = typer.Option(7, help="Giorni di embargo tra train e finestra"),
) -> None:
    with job_run("job_skill") as stats:
        stations = _primary_stations(config_dir)

        con = duckdb.connect(str(db), read_only=True)
        df = _load_features(con)
        primary = _load_primary_obs(con, stations)
        con.close()

        win_start = pd.to_datetime(window_start).date()
        cutoff = (pd.Timestamp(win_start) - pd.Timedelta(days=embargo_days)).date()

        train = df[df["target_date"] <= cutoff]
        models = {}
        for var in SKILL_VARS:
            col = f"target_{var}"
            mask = train[col].notna()
            models[var] = _train_lgbm(train.loc[mask, FEATURE_COLS], train.loc[mask, col], 0.50)
        logger.info(f"Train q=0.50: {len(train)} righe ≤ {cutoff} | test ≥ {win_start}")

        test = df[df["target_date"] >= win_start].copy()
        # Predici prima del merge: la merge con primary altera il dtype categorical di
        # location_id e LightGBM rifiuta categorie non allineate al training (KI nota).
        for var in SKILL_VARS:
            test[f"pred_{var}"] = models[var].predict(test[FEATURE_COLS])
        test = test.merge(primary, on=["location_id", "target_date"], how="left")

        # Onestà: la finestra finisce all'ultima data con osservazione reale, non alle
        # date forecast future presenti in features_daily (già escluse dagli n per-lead).
        evaluated = primary[primary["target_date"] >= win_start]["target_date"]
        window_end = max(evaluated) if len(evaluated) else win_start
        locations: dict[str, object] = {}
        for loc_id in sorted(stations):
            loc_test = test[test["location_id"] == loc_id]
            locations[loc_id] = {
                "sir_station_id": stations[loc_id],
                **{var: _curve_for(loc_test, var) for var in SKILL_VARS},
            }

        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "ground_truth": "sir_primary",
            "window_start": win_start.isoformat(),
            "window_end": window_end.isoformat(),
            "embargo_days": embargo_days,
            "leads_h": LEADS,
            "min_samples_per_lead": MIN_SAMPLES_PER_LEAD,
            "locations": locations,
        }

        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / "skill.json"
        # Scrittura atomica: nginx serve il file mentre il cron lo riscrive.
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        tmp.replace(out)
        logger.info(f"skill.json scritto: {out} ({len(locations)} location)")
        stats.rows = len(locations)
        stats.summary = f"skill.json: {len(locations)} location"


if __name__ == "__main__":
    app()
