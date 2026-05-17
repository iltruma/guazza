"""Entry point cron — predizioni ML + DLE + output JSON.

Pipeline per ogni location (ordine):
  1. ensure_predictions_schema()    — migrazione schema v0.5 se necessario
  2. backfill_prediction_obs()      — riempie *_obs su predictions passate
  3. query features_daily           — ultima riga disponibile per location
  4. models.predict()               — quantile CI tmin/tmax/precip
  5. upsert_predictions()           — salva in DuckDB
  6. build_signals() + DLE          — valuta indicatori
  7. log_results()                  — indicator_log
  8. compute_coverage_30d()         — copertura empirica rolling
  9. write_location_json()          — {output_dir}/{location_id}.json

Cron: ogni 6h, subito dopo il job forecasts + features build.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd
import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.indicators import evaluate_all, load_indicators, log_results
from guazza.models import load_artifacts, predict
from guazza.output import build_signals, compute_coverage_30d, write_location_json
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


@app.command("run")
def cmd_run(
    db_path:    Path = typer.Option(_DB_PATH,    "--db",         help="Path DuckDB"),
    model_dir:  Path = typer.Option(_MODEL_DIR,  "--model-dir",  help="Directory artefatti modello"),
    output_dir: Path = typer.Option(_OUTPUT_DIR, "--output-dir", help="Directory output JSON"),
    dry_run:    bool = typer.Option(False,       "--dry-run",    help="Non scrive su disco né in DB"),
) -> None:
    """Genera predizioni ML + indicatori DLE per tutte le location."""
    setup_logging()
    _ping_healthchecks("/start")

    try:
        artifacts = load_artifacts(model_dir=model_dir)
        model_version = artifacts.trained_at.strftime("%Y%m%d")
        logger.info(f"Artefatti caricati: {model_version}, {len(artifacts.targets)} target")

        indicators_cfg = load_indicators()

        with DuckDBClient(db_path=db_path) as db:
            # Schema migration e backfill obs prima di qualsiasi predict
            if not dry_run:
                db.ensure_predictions_schema()
                n_backfilled = db.backfill_prediction_obs()
                if n_backfilled:
                    logger.info(f"Obs backfilled: {n_backfilled} predictions aggiornate")

            # Ultima riga disponibile per ogni location (data più recente, lead time massimo)
            df_predict = db.execute("""
                SELECT *
                FROM features_daily
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY location_id
                    ORDER BY target_date DESC, lead_time_h DESC
                ) = 1
            """).df()

            if df_predict.empty:
                logger.error("features_daily vuota — esegui prima: features build")
                raise typer.Exit(1)

            today = datetime.now(tz=UTC).date()
            json_paths: list[Path] = []

            for _, row in df_predict.iterrows():
                location_id = str(row["location_id"])
                target_date = row["target_date"]
                lead_time_h = int(row["lead_time_h"])

                if hasattr(target_date, "date"):
                    target_date_obj = target_date.date() if hasattr(target_date, "date") else target_date
                else:
                    target_date_obj = target_date
                if target_date_obj < today:
                    logger.warning(
                        f"[{location_id}] target_date {target_date_obj} è nel passato "
                        f"— in produzione esegui prima il job forecasts"
                    )

                # Feature vector per il modello
                X = pd.DataFrame([row[artifacts.feature_cols]])
                X["location_id"] = X["location_id"].astype("category")

                pred = predict(artifacts, X, lead_h=lead_time_h)

                # Segnali per il DLE: ML quantile + NWP ensemble + obs real-time
                obs_summary = _fetch_obs_summary(db, location_id)
                signals = build_signals(pred, row, obs_summary)

                # Valutazione indicatori
                results = evaluate_all(indicators_cfg, signals, location_id)

                logger.info(
                    f"[{location_id}] {target_date_obj} lead={lead_time_h}h "
                    + " ".join(f"{r.indicator_id}={r.verdict[0]}" for r in results)
                )

                if dry_run:
                    continue

                # Persistenza DuckDB
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

                # Coverage e output JSON
                coverage = compute_coverage_30d(db, location_id)
                path = write_location_json(
                    location_id=location_id,
                    target_date=str(target_date_obj),
                    lead_time_h=lead_time_h,
                    pred=pred,
                    indicators=results,
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
