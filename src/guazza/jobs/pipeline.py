"""Job CLI: pipeline 6h — forecasts → features → predict → skill-history.

Unico CronJob schedulato ogni 6h (suggerito: 02/08/14/20 UTC, ~2h dopo i run ECMWF).
Sostituisce i job separati guazza-predict, guazza-features, guazza-skill-history.

Passi in sequenza sulla stessa connessione DuckDB:
  1. Open-Meteo forecast (tutti i modelli, 7 giorni)
  2. build_features_daily()
  3. predizioni ML + ACI + DLE + JSON output
  4. skill-history append (ieri) + dump JSON

Uso:
    uv run python -m guazza.jobs.pipeline run
    uv run python -m guazza.jobs.pipeline run --dry-run
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.features import build_features_daily
from guazza.fetch_openmeteo import fetch_openmeteo_all_locations
from guazza.indicators import evaluate_all, load_indicators, log_results
from guazza.jobs._common import (
    CONFIG_DIR_OPTION,
    DB_OPTION,
    OUTPUT_DIR_OPTION,
    job_run,
    ping_healthchecks,
)
from guazza.models import (
    LEAD_BUCKETS,
    TARGETS,
    AdaptiveConformalizer,
    _lead_time_bucket,
    apply_aci_correction,
    get_aci_pair,
    load_artifacts,
    predict_frame,
)
from guazza.monitor import check_and_log, compute_coverage
from guazza.output import (
    build_signals,
    build_signals_today,
    compute_coverage_30d,
    compute_hourly_profile,
    expected_precip,
    get_current_conditions,
    get_daily_weather_code,
    get_nwp_model_comparison,
    write_location_json,
)
from guazza.skill_history import (
    DEFAULT_DUMP_PATH,
    append_one,
    atomic_write_json,
    dump_payload,
)
from guazza.storage import DuckDBClient
from guazza.weights import load_configs, refresh_station_weights, refresh_upstream_rings

app = typer.Typer(help="Pipeline 6h: forecasts → features → predict → skill-history.")

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


def _aci_update_from_history(db: DuckDBClient) -> int:
    rows = db.execute("""
        SELECT ts_valid, lead_time_h,
               tmin_p10, tmin_p90, tmin_p05, tmin_p95, tmin_obs,
               tmax_p10, tmax_p90, tmax_p05, tmax_p95, tmax_obs,
               precip_p10, precip_p90, precip_p05, precip_p95, precip_obs
        FROM predictions
        WHERE tmin_obs IS NOT NULL OR tmax_obs IS NOT NULL OR precip_obs IS NOT NULL
        ORDER BY ts_valid
    """).df()

    if rows.empty:
        return 0

    target_obs = {
        "tmin_c":    ("tmin_obs",   "tmin_p10",   "tmin_p90",   "tmin_p05",   "tmin_p95"),
        "tmax_c":    ("tmax_obs",   "tmax_p10",   "tmax_p90",   "tmax_p05",   "tmax_p95"),
        "precip_mm": ("precip_obs", "precip_p10", "precip_p90", "precip_p05", "precip_p95"),
    }

    rows = rows.assign(_bucket=rows["lead_time_h"].apply(_lead_time_bucket))

    n_updated = 0
    for target, (obs_col, p10_col, p90_col, p05_col, p95_col) in target_obs.items():
        for bucket in LEAD_BUCKETS:
            aci_80 = AdaptiveConformalizer(alpha_target=0.20, learning_rate=0.02)
            aci_90 = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.02)
            for _, row in rows.iterrows():
                if row["_bucket"] != bucket:
                    continue
                actual = row[obs_col]
                if pd.isna(actual):
                    continue
                if pd.isna(row[p10_col]) or pd.isna(row[p90_col]) or pd.isna(row[p05_col]) or pd.isna(row[p95_col]):
                    continue
                aci_80.update(bool(row[p10_col] <= actual <= row[p90_col]))
                aci_90.update(bool(row[p05_col] <= actual <= row[p95_col]))
            db.upsert_aci_state(
                target, bucket,
                aci_80.alpha_t, aci_90.alpha_t,
                aci_80.n_updates,
                aci_80.err_sum, aci_90.err_sum,
            )
            n_updated += 1
    return n_updated


@app.command("run")
def cmd_run(
    db_path:      Path = DB_OPTION,
    config_dir:   Path = CONFIG_DIR_OPTION,
    model_dir:    Path = typer.Option(_MODEL_DIR, "--model-dir", help="Directory artefatti modello"),
    output_dir:   Path = OUTPUT_DIR_OPTION,
    forecast_days: int = typer.Option(7, "--forecast-days", help="Giorni di forecast Open-Meteo (1-16)"),
    skill_output: Path = typer.Option(DEFAULT_DUMP_PATH, "--skill-output", help="Path skill_history.json"),
    dry_run:      bool = typer.Option(False, "--dry-run", help="Non scrive su disco né in DB"),
) -> None:
    """Pipeline 6h: forecasts → features → predict → skill-history append + dump."""
    with job_run("job_pipeline") as stats:
        locations, stations = load_configs(config_dir)

        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            # ── 0. Pesi stazione→location (prerequisito features/predict) ────
            # Ricalcolati da config ad ogni run: idempotente (DELETE+INSERT),
            # costo ~0 (nessun fetch esterno). Elimina il refresh manuale
            # richiesto su DB nuovo o dopo modifiche a locations/stations.yaml.
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
                logger.info(f"pipeline forecasts: {fc_total} record")

            # ── 2. Features ─────────────────────────────────────────────────
            n_features = build_features_daily(db)
            logger.info(f"pipeline features: {n_features} righe in features_daily")

            # ── 3. Predict ──────────────────────────────────────────────────
            if not dry_run:
                n_backfilled = db.backfill_prediction_obs()
                if n_backfilled:
                    logger.info(f"pipeline obs backfill: {n_backfilled} predictions")
                n_bench = db.backfill_benchmark_obs()
                if n_bench:
                    logger.info(f"pipeline bench backfill: {n_bench} benchmark")
                n_aci = _aci_update_from_history(db)
                if n_aci:
                    logger.info(f"pipeline ACI: {n_aci} coppie aggiornate")

            artifacts = load_artifacts(model_dir=model_dir)
            model_version = artifacts.trained_at.strftime("%Y%m%d")
            logger.info(f"pipeline modello: {model_version}, {len(artifacts.targets)} target")

            indicators_cfg = load_indicators()

            df_all = db.execute("""
                SELECT *
                FROM features_daily
                WHERE target_date >= CURRENT_DATE
                QUALIFY ROW_NUMBER() OVER (
                    PARTITION BY location_id, target_date
                    ORDER BY lead_time_h ASC
                ) = 1
                ORDER BY location_id, target_date
            """).df()

            if df_all.empty:
                logger.warning("Nessuna data futura in features_daily dopo forecasts+build — skip predict")
                return

            json_paths: list[Path] = []

            for location_id, loc_df in df_all.groupby("location_id"):
                location_id = str(location_id)
                obs_summary = _fetch_obs_summary(db, location_id)
                day_entries: list[dict[str, Any]] = []

                loc_df = loc_df.reset_index(drop=True)
                lead_times = [int(v) for v in loc_df["lead_time_h"]]
                preds = predict_frame(artifacts, loc_df[artifacts.feature_cols], lead_times)

                aci_cache: dict[tuple[str, str], tuple] = {}

                for i, row in loc_df.iterrows():
                    target_date_obj = _to_date(row["target_date"])
                    lead_time_h = lead_times[i]
                    pred = preds[i]
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

                    if target_date_obj == date.today():
                        current_obs = get_current_conditions(db, location_id)
                        signals = build_signals_today(pred, row, obs_summary, current_obs)
                    else:
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
                        precip_anchor=expected_precip(pred["precip_mm"]),
                        tmin_ci80_lo=pred["tmin_c"].get("ci80_lo"),
                        tmin_ci80_hi=pred["tmin_c"].get("ci80_hi"),
                        tmax_ci80_lo=pred["tmax_c"].get("ci80_lo"),
                        tmax_ci80_hi=pred["tmax_c"].get("ci80_hi"),
                        precip_ci80_lo=pred["precip_mm"].get("ci80_lo"),
                        precip_ci80_hi=pred["precip_mm"].get("ci80_hi"),
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

                    if dry_run:
                        continue

                    ts_valid = datetime(
                        target_date_obj.year, target_date_obj.month, target_date_obj.day,
                        tzinfo=None,
                    )
                    db.upsert_benchmark_forecasts([{
                        "source":      cmp["source"],
                        "location_id": location_id,
                        "target_date": target_date_obj,
                        "lead_time_h": lead_time_h,
                        "tmin_c":      cmp["tmin_c"],
                        "tmax_c":      cmp["tmax_c"],
                        "precip_mm":   cmp["precip_mm"],
                    } for cmp in nwp_comparison])
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
                    db=db,
                )
                json_paths.append(path)

            logger.info(f"pipeline predict: {len(json_paths)} JSON scritti")

            # ── 4. Skill-history append (ieri) + dump ────────────────────────
            n_sh = 0
            if not dry_run:
                assert db._conn is not None
                yesterday = date.today() - timedelta(days=1)
                n_sh = append_one(db._conn, yesterday)
                logger.info(f"pipeline skill-history: {n_sh} righe upsert ({yesterday})")

                payload = dump_payload(db._conn)
                atomic_write_json(skill_output, payload)
                logger.info(f"pipeline skill-history dump: {skill_output}")

            # ── 5. Monitor coverage ACI ──────────────────────────────────────
            coverage_results = compute_coverage(db)
            if not coverage_results:
                logger.warning("pipeline monitor: nessuna prediction con actual negli ultimi 30gg")
            else:
                n_alerts = check_and_log(coverage_results)
                if n_alerts > 0 and not dry_run:
                    ping_healthchecks("/fail")
                    logger.warning(f"pipeline monitor: drift su {n_alerts} combinazioni — healthchecks /fail")

        stats.rows = len(json_paths)
        n_sh_str = str(n_sh) if not dry_run else "dry"
        stats.summary = f"forecasts→features({n_features})→predict({len(json_paths)} JSON)→skill-history({n_sh_str})"


if __name__ == "__main__":
    app()
