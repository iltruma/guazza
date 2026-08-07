"""Job CLI: forecast — NWP live → features → predict → JSON location.

Gira ogni 6h (02/08/14/20 UTC, ~2h dopo i run ECMWF).
Risponde a: "cosa prevedo per oggi e i prossimi 7 giorni?"

Passi in sequenza sulla stessa connessione DuckDB:
  1. Refresh pesi stazione→location da config (idempotente)
  2. Fetch forecast NWP live Open-Meteo (4 modelli, 7 giorni)
  3. build_features_daily()
  4. load_artifacts() — carica modello allenato da review
  5. Predict su tutte le righe future (tutti i lead) → upsert predictions + benchmark + JSON

Uso:
    uv run python -m guazza.jobs.forecast run
    uv run python -m guazza.jobs.forecast run --dry-run
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.aci import apply_aci_correction, get_aci_pair
from guazza.db_queries import (
    compute_coverage_30d,
    get_current_conditions,
    get_daily_weather_code,
    get_nwp_model_comparison,
)
from guazza.features import build_features_daily
from guazza.fetch_openmeteo import fetch_openmeteo_all_locations
from guazza.hourly_corrector import load_corrector
from guazza.indicators import evaluate_all, load_indicators, log_results
from guazza.jobs._common import (
    CONFIG_DIR_OPTION,
    DB_OPTION,
    OUTPUT_DIR_OPTION,
    job_run,
)
from guazza.models import (
    TARGETS,
    _lead_time_bucket,
    load_artifacts,
    predict_frame,
)
from guazza.output import (
    build_signals,
    build_signals_today,
    compute_hourly_profile,
    expected_precip,
    write_location_json,
)
from guazza.storage import DuckDBClient
from guazza.weights import load_configs, refresh_station_weights, refresh_upstream_rings

app = typer.Typer(help="Forecast 6h: NWP live → features → predict → JSON.")

_MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/var/lib/guazza/models"))


@app.callback()
def _callback() -> None:
    setup_logging()


def _fetch_obs_summary(db: DuckDBClient, location_id: str) -> dict[str, float | None]:
    level_row = db.execute("""
        SELECT level_m FROM observations
        WHERE location_id = ? AND level_m IS NOT NULL
        ORDER BY ts DESC LIMIT 1
    """, [location_id]).fetchone()
    return {
        "level_sir": level_row[0] if level_row else None,
    }


def _to_date(val: Any) -> Any:
    return val.date() if hasattr(val, "date") else val


@app.command("run")
def cmd_run(
    db_path:       Path = DB_OPTION,
    config_dir:    Path = CONFIG_DIR_OPTION,
    model_dir:     Path = typer.Option(_MODEL_DIR, "--model-dir", help="Directory artefatti modello"),
    output_dir:    Path = OUTPUT_DIR_OPTION,
    forecast_days: int  = typer.Option(7, "--forecast-days", help="Giorni di forecast Open-Meteo (1-16)"),
    dry_run:       bool = typer.Option(False, "--dry-run", help="Non scrive su disco né in DB"),
) -> None:
    """Forecast 6h: NWP live → features → predict → JSON."""
    with job_run("job_forecast") as stats:
        locations, stations = load_configs(config_dir)

        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            # ── 0. Pesi stazione→location (prerequisito features/predict) ────
            # Ricalcolati da config ad ogni run: idempotente (DELETE+INSERT),
            # costo ~0 (nessun fetch esterno).
            if not dry_run:
                refresh_station_weights(db, locations, stations)
                refresh_upstream_rings(db, locations, stations)

            # ── 1. Forecasts ────────────────────────────────────────────────
            if not dry_run:
                all_results = fetch_openmeteo_all_locations(
                    locations=locations,
                    forecast_days=forecast_days,
                )
                fc_total = 0
                for model_results in all_results.values():
                    for records in model_results.values():
                        if records:
                            fc_total += db.upsert_forecasts(records)
                logger.info(f"forecast forecasts: {fc_total} record")

            # ── 2. Features ─────────────────────────────────────────────────
            n_features = build_features_daily(db)
            logger.info(f"forecast features: {n_features} righe in features_daily")

            # ── 3. Predict ──────────────────────────────────────────────────
            artifacts = load_artifacts(model_dir=model_dir)
            model_version = artifacts.trained_at.strftime("%Y%m%d")
            logger.info(f"forecast modello: {model_version}, {len(artifacts.targets)} target")

            indicators_cfg = load_indicators()

            df_all = db.execute("""
                SELECT *
                FROM features_daily
                WHERE target_date >= CURRENT_DATE
                ORDER BY location_id, target_date, lead_time_h ASC
            """).df()

            if df_all.empty:
                logger.warning("Nessuna data futura in features_daily dopo forecasts+build — skip predict")
                return

            json_paths: list[Path] = []

            corrector = load_corrector(model_dir)
            if corrector is not None:
                logger.info(f"hourly corrector attivo: {model_dir / 'hourly_corrector.lgb'}")

            for location_id, loc_df in df_all.groupby("location_id"):
                location_id = str(location_id)
                obs_summary = _fetch_obs_summary(db, location_id)
                day_entries: list[dict[str, Any]] = []

                loc_df = loc_df.reset_index(drop=True)
                lead_times = [int(v) for v in loc_df["lead_time_h"]]
                preds = predict_frame(artifacts, loc_df[artifacts.feature_cols], lead_times)

                aci_cache: dict[tuple[str, str], tuple] = {}

                # Prima riga per target_date = lead minimo (query già ordinata per lead_time_h ASC)
                min_lead_indices: set[int] = set(
                    loc_df.groupby("target_date")["lead_time_h"].idxmin().tolist()
                )

                prediction_records: list[dict] = []

                for i, row in loc_df.iterrows():
                    target_date_obj = _to_date(row["target_date"])
                    lead_time_h = lead_times[int(i)]
                    pred = preds[int(i)]
                    bucket = _lead_time_bucket(lead_time_h)

                    if not dry_run:
                        for target in TARGETS:
                            key = (target, bucket)
                            if key not in aci_cache:
                                aci_cache[key] = get_aci_pair(db, target, bucket)
                            aci_80, aci_90 = aci_cache[key]
                            new_lo80, new_hi80, new_lo90, new_hi90, _ = apply_aci_correction(
                                pred[target]["ci80_lo"], pred[target]["ci80_hi"],
                                pred[target]["ci90_lo"], pred[target]["ci90_hi"],
                                aci_80, aci_90,
                            )
                            pred[target]["ci80_lo"] = new_lo80
                            pred[target]["ci80_hi"] = new_hi80
                            pred[target]["ci90_lo"] = new_lo90
                            pred[target]["ci90_hi"] = new_hi90

                    logger.info(
                        f"[{location_id}] {target_date_obj} lead={lead_time_h}h"
                    )

                    if int(i) in min_lead_indices:
                        if target_date_obj == date.today():
                            current_obs = get_current_conditions(db, location_id)
                            signals = build_signals_today(pred, row, obs_summary, current_obs)
                        else:
                            signals = build_signals(pred, row, obs_summary)
                        results = evaluate_all(indicators_cfg, signals, location_id)

                        hourly = compute_hourly_profile(
                            db, location_id, str(target_date_obj),
                            tmin_p50=pred["tmin_c"].get("p50"),
                            tmax_p50=pred["tmax_c"].get("p50"),
                            precip_anchor=expected_precip(pred["precip_mm"]),
                            tmin_ci80_lo=pred["tmin_c"].get("ci80_lo"),
                            tmin_ci80_hi=pred["tmin_c"].get("ci80_hi"),
                            tmax_ci80_lo=pred["tmax_c"].get("ci80_lo"),
                            tmax_ci80_hi=pred["tmax_c"].get("ci80_hi"),
                            precip_ci80_lo=pred["precip_mm"].get("ci80_lo"),
                            precip_ci80_hi=pred["precip_mm"].get("ci80_hi"),
                            corrector=corrector,
                        )
                        nwp_comparison = get_nwp_model_comparison(db, location_id, str(target_date_obj))
                        weather_code = get_daily_weather_code(db, location_id, str(target_date_obj))

                        day_entries.append({
                            "target_date":    str(target_date_obj),
                            "lead_time_h":    lead_time_h,
                            "pred":           pred,
                            "indicators":     results,
                            "hourly":         hourly,
                            "nwp_comparison": nwp_comparison,
                            "weather_code":   weather_code,
                        })

                        if not dry_run:
                            db.upsert_benchmark_forecasts([{
                                "source":      cmp["source"],
                                "location_id": location_id,
                                "target_date": target_date_obj,
                                "lead_time_h": lead_time_h,
                                "tmin_c":      cmp["tmin_c"],
                                "tmax_c":      cmp["tmax_c"],
                                "precip_mm":   cmp["precip_mm"],
                            } for cmp in nwp_comparison])
                            log_results(db, results, input_summary={
                                k: v for k, v in signals.items() if v is not None
                            })

                    if dry_run:
                        continue

                    ts_valid = datetime(
                        target_date_obj.year, target_date_obj.month, target_date_obj.day,
                        tzinfo=None,
                    )
                    prediction_records.append({
                        "model_version": model_version,
                        "location_id":   location_id,
                        "ts_valid":      ts_valid,
                        "lead_time_h":   lead_time_h,
                        "tmin_c":        pred["tmin_c"],
                        "tmax_c":        pred["tmax_c"],
                        "precip_mm":     pred["precip_mm"],
                        "rain_prob":     pred.get("rain_clf", {}).get("prob_rain"),
                    })

                if dry_run:
                    continue

                if prediction_records:
                    db.upsert_predictions(prediction_records)

                coverage = compute_coverage_30d(db, location_id)
                path = write_location_json(
                    location_id=location_id,
                    days=day_entries,
                    coverage=coverage,
                    output_dir=output_dir,
                    db=db,
                )
                json_paths.append(path)

            logger.info(f"forecast predict: {len(json_paths)} JSON scritti")

        stats.rows = len(json_paths)
        stats.summary = f"forecasts→features({n_features})→predict({len(json_paths)} JSON)"


if __name__ == "__main__":
    app()
