"""Entry point cron — predizioni ML + DLE + output JSON.

Pipeline per ogni location (ordine):
  1. ensure_predictions_schema()    — migrazione schema v0.5 se necessario
  2. backfill_prediction_obs()      — riempie *_obs su predictions passate
  3. query features_daily           — miglior lead_time_h per ogni (location, data futura)
  4. per ogni (location, data):
       models.predict()             — quantile CI tmin/tmax/precip
       upsert_predictions()         — salva in DuckDB
       build_signals() + DLE        — valuta indicatori
       log_results()                — indicator_log
  5. per ogni location:
       compute_coverage_30d()       — copertura empirica rolling
       write_location_json()        — {output_dir}/{location_id}.json (tutti i giorni)

Cron: ogni 6h, subito dopo il job forecasts + features build.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.indicators import evaluate_all, load_indicators, log_results
from guazza.models import load_artifacts, predict
from guazza.output import (
    build_signals,
    compute_coverage_30d,
    compute_hourly_profile,
    get_nwp_model_comparison,
    write_location_json,
)
from guazza.storage import DuckDBClient

app = typer.Typer(help="Predizioni ML per Guazza.")

_DB_PATH     = Path(os.environ.get("DB_PATH",     "/var/lib/guazza/guazza.duckdb"))
_MODEL_DIR   = Path(os.environ.get("MODEL_DIR",   "/var/lib/guazza/models"))
_OUTPUT_DIR  = Path(os.environ.get("OUTPUT_DIR",  "data/output"))


def _ping_healthchecks(status: str = "") -> None:
    base_url = os.environ.get("HEALTHCHECKS_URL", "").strip()
    if not base_url:
        return
    url = base_url.rstrip("/") + status
    try:
        httpx.get(url, timeout=5)
        logger.debug(f"Healthchecks ping: {url}")
    except Exception as e:
        logger.warning(f"Healthchecks ping fallito: {e}")


def _fetch_obs_summary(db: DuckDBClient, location_id: str) -> dict[str, float | None]:
    """Ultima lettura idrometrica e PM10 disponibile per una location."""
    level_row = db.execute("""
        SELECT level_m FROM observations
        WHERE location_id = ? AND level_m IS NOT NULL
        ORDER BY ts DESC LIMIT 1
    """, [location_id]).fetchone()

    pm10_row = db.execute("""
        SELECT pm10_ugm3 FROM observations
        WHERE location_id = ? AND pm10_ugm3 IS NOT NULL
        ORDER BY ts DESC LIMIT 1
    """, [location_id]).fetchone()

    return {
        "level_sir":      level_row[0] if level_row else None,
        "pm10_predicted": pm10_row[0] if pm10_row else None,
    }


def _to_date(val: Any) -> Any:
    return val.date() if hasattr(val, "date") else val


@app.command("run")
def cmd_run(
    db_path:    Path = typer.Option(_DB_PATH,    "--db",         help="Path DuckDB"),
    model_dir:  Path = typer.Option(_MODEL_DIR,  "--model-dir",  help="Directory artefatti modello"),
    output_dir: Path = typer.Option(_OUTPUT_DIR, "--output-dir", help="Directory output JSON"),
    dry_run:    bool = typer.Option(False,       "--dry-run",    help="Non scrive su disco né in DB"),
) -> None:
    """Genera predizioni ML + indicatori DLE per tutte le location (D+1…D+7)."""
    setup_logging()
    _ping_healthchecks("/start")

    try:
        artifacts = load_artifacts(model_dir=model_dir)
        model_version = artifacts.trained_at.strftime("%Y%m%d")
        logger.info(f"Artefatti caricati: {model_version}, {len(artifacts.targets)} target")

        indicators_cfg = load_indicators()

        with DuckDBClient(db_path=db_path) as db:
            if not dry_run:
                db.ensure_predictions_schema()
                n_backfilled = db.backfill_prediction_obs()
                if n_backfilled:
                    logger.info(f"Obs backfilled: {n_backfilled} predictions aggiornate")

            # Miglior lead_time_h per ogni (location_id, target_date) futuro
            df_all = db.execute("""
                SELECT *
                FROM features_daily
                WHERE target_date > CURRENT_DATE
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY location_id, target_date
                    ORDER BY lead_time_h DESC
                ) = 1
                ORDER BY location_id, target_date
            """).df()

            if df_all.empty:
                logger.warning(
                    "Nessuna data futura in features_daily "
                    "— esegui prima il job forecasts + features build"
                )
                _ping_healthchecks()
                return

            json_paths: list[Path] = []

            for location_id, loc_df in df_all.groupby("location_id"):
                location_id = str(location_id)
                obs_summary = _fetch_obs_summary(db, location_id)
                day_entries: list[dict[str, Any]] = []

                for _, row in loc_df.iterrows():
                    target_date_obj = _to_date(row["target_date"])
                    lead_time_h = int(row["lead_time_h"])

                    X = pd.DataFrame([row[artifacts.feature_cols]])
                    X["location_id"] = X["location_id"].astype("category")
                    pred = predict(artifacts, X, lead_h=lead_time_h)

                    signals = build_signals(pred, row, obs_summary)
                    results = evaluate_all(indicators_cfg, signals, location_id)

                    logger.info(
                        f"[{location_id}] {target_date_obj} lead={lead_time_h}h "
                        + " ".join(f"{r.indicator_id}={r.verdict[0]}" for r in results)
                    )

                    hourly = compute_hourly_profile(
                        db, location_id, str(target_date_obj),
                        tmin_p50=pred["tmin_c"].get("p50"),
                        tmax_p50=pred["tmax_c"].get("p50"),
                        precip_p50=pred["precip_mm"].get("p50"),
                    )

                    nwp_comparison = get_nwp_model_comparison(
                        db, location_id, str(target_date_obj),
                    )

                    day_entries.append({
                        "target_date":    str(target_date_obj),
                        "lead_time_h":    lead_time_h,
                        "pred":           pred,
                        "indicators":     results,
                        "hourly":         hourly,
                        "nwp_comparison": nwp_comparison,
                    })

                    if dry_run:
                        continue

                    ts_valid = datetime(
                        target_date_obj.year, target_date_obj.month, target_date_obj.day,
                        tzinfo=None,
                    )
                    db.upsert_predictions([{
                        "model_version": model_version,
                        "location_id":   location_id,
                        "ts_valid":      ts_valid,
                        "lead_time_h":   lead_time_h,
                        "tmin_c":        pred["tmin_c"],
                        "tmax_c":        pred["tmax_c"],
                        "precip_mm":     pred["precip_mm"],
                    }])
                    log_results(db, results, input_summary={
                        k: v for k, v in signals.items() if v is not None
                    })

                if dry_run:
                    continue

                coverage = compute_coverage_30d(db, location_id)
                path = write_location_json(
                    location_id=location_id,
                    days=day_entries,
                    coverage=coverage,
                    output_dir=output_dir,
                )
                json_paths.append(path)

        if not dry_run:
            logger.info(f"JSON scritti: {[str(p) for p in json_paths]}")
        _ping_healthchecks()

    except Exception:
        logger.exception("predict run fallito")
        _ping_healthchecks("/fail")
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
