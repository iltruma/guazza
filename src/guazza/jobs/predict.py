"""Entry point cron — predizioni ML + DLE + output JSON.

Pipeline per ogni location (ordine):
  1. ensure_*_schema()                — migrazioni idempotenti
  2. backfill_prediction_obs()        — riempie *_obs su predictions passate
  3. update_aci_from_history()        — Adaptive Conformal Inference: aggiorna
                                        alpha_t su predizioni passate con actual
  4. query features_daily             — miglior lead_time_h per ogni (location, data futura)
  5. per ogni (location, data):
        models.predict()               — quantile CI tmin/tmax/precip (CQR)
        apply_aci_correction()         — riscala CI bounds con ACI alpha_t corrente
        upsert_predictions()           — salva in DuckDB
        build_signals() + DLE          — valuta indicatori
        log_results()                  — indicator_log
  6. per ogni location:
        compute_coverage_30d()         — copertura empirica rolling
        write_location_json()          — {output_dir}/{location_id}.json (tutti i giorni)

Cron: ogni 6h, subito dopo il job forecasts + features build.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.indicators import evaluate_all, load_indicators, log_results
from guazza.jobs._common import DB_OPTION, OUTPUT_DIR_OPTION, job_run
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
from guazza.storage import DuckDBClient

app = typer.Typer(help="Predizioni ML per Guazza.")

_MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/var/lib/guazza/models"))


@app.callback()
def _callback() -> None:
    setup_logging()


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


def _aci_update_from_history(db: DuckDBClient) -> int:
    """Aggiorna ACI su TUTTE le predictions passate con actual valorizzato.

    Per ogni (target, lead_bucket) crea istanze ACI fresche (`alpha_t = alpha_target`)
    e itera sull'intera storia in ordine cronologico. Salva lo state finale.
    Non carica lo state precedente: è deterministico e idempotente (gli stessi
    dati → stessa evoluzione di `alpha_t`). Il costo è O(N_predictions) per run,
    ~1s su 20k righe.

    Returns:
        Numero di coppie (target, bucket) aggiornate.
    """
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
        "tmin_c":    ("tmin_obs",    "tmin_p10",    "tmin_p90",    "tmin_p05",    "tmin_p95"),
        "tmax_c":    ("tmax_obs",    "tmax_p10",    "tmax_p90",    "tmax_p05",    "tmax_p95"),
        "precip_mm": ("precip_obs",  "precip_p10",  "precip_p90",  "precip_p05",  "precip_p95"),
    }

    # Assegna bucket label una volta sola (usato da tutti i loop interni).
    from guazza.models import _lead_time_bucket as _bucket_fn
    rows = rows.assign(
        _bucket=rows["lead_time_h"].apply(_bucket_fn),
    )

    n_updated = 0
    for target, (obs_col, p10_col, p90_col, p05_col, p95_col) in target_obs.items():
        for bucket in LEAD_BUCKETS:
            # Fresh ACI: parte da alpha_target e itera su tutta la storia.
            # Idempotente: lo stesso set di dati → stessa evoluzione di alpha_t.
            aci_80 = AdaptiveConformalizer(alpha_target=0.20, learning_rate=0.02)
            aci_90 = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.02)

            for _, row in rows.iterrows():
                # Filtro per bucket: ogni ACI riceve solo i feedback del suo lead time.
                if row["_bucket"] != bucket:
                    continue
                actual = row[obs_col]
                if pd.isna(actual):
                    continue
                # Guardia contro NaN nei quantili (dati corrotti o inserimenti parziali).
                if pd.isna(row[p10_col]) or pd.isna(row[p90_col]) or pd.isna(row[p05_col]) or pd.isna(row[p95_col]):
                    continue

                cov_80 = (row[p10_col] <= actual <= row[p90_col])
                cov_90 = (row[p05_col] <= actual <= row[p95_col])
                aci_80.update(bool(cov_80))
                aci_90.update(bool(cov_90))

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
    db_path:    Path = DB_OPTION,
    model_dir:  Path = typer.Option(_MODEL_DIR, "--model-dir", help="Directory artefatti modello"),
    output_dir: Path = OUTPUT_DIR_OPTION,
    dry_run:    bool = typer.Option(False, "--dry-run", help="Non scrive su disco né in DB"),
) -> None:
    """Genera predizioni ML + indicatori DLE per tutte le location (D+0…D+7)."""
    with job_run("job_predict") as stats:
        artifacts = load_artifacts(model_dir=model_dir)
        model_version = artifacts.trained_at.strftime("%Y%m%d")
        logger.info(f"Artefatti caricati: {model_version}, {len(artifacts.targets)} target")

        indicators_cfg = load_indicators()

        with DuckDBClient(db_path=db_path) as db:
            if not dry_run:
                # Idempotente: garantisce tabelle + vista obs_weighted_daily (usata
                # dai backfill *_obs) anche su un DB che non ha rieseguito lo schema.
                db.init_schema()
                db.ensure_predictions_schema()
                db.ensure_benchmark_schema()
                db.ensure_aci_schema()
                n_backfilled = db.backfill_prediction_obs()
                if n_backfilled:
                    logger.info(f"Obs backfilled: {n_backfilled} predictions aggiornate")
                n_bench_backfilled = db.backfill_benchmark_obs()
                if n_bench_backfilled:
                    logger.info(f"Obs backfilled: {n_bench_backfilled} benchmark aggiornati")
                # ACI: aggiorna alpha_t su predizioni passate con actual (Sprint 9)
                n_aci = _aci_update_from_history(db)
                if n_aci:
                    logger.info(f"ACI aggiornato: {n_aci} coppie (target, lead_bucket)")

            # Per ogni (location, data): forecast più recente = lead_time_h più corto
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
                logger.warning(
                    "Nessuna data futura in features_daily "
                    "— esegui prima il job forecasts + features build"
                )
                return

            json_paths: list[Path] = []

            for location_id, loc_df in df_all.groupby("location_id"):
                location_id = str(location_id)
                obs_summary = _fetch_obs_summary(db, location_id)
                day_entries: list[dict[str, Any]] = []

                # Predizione in batch per tutta la location: una chiamata-modello per
                # (target, quantile) invece di una per giorno (output identico).
                loc_df = loc_df.reset_index(drop=True)
                lead_times = [int(v) for v in loc_df["lead_time_h"]]
                preds = predict_frame(
                    artifacts, loc_df[artifacts.feature_cols], lead_times
                )

                # Cache ACI per (target, lead_bucket) — un caricamento per bucket
                # invece di uno per riga.
                aci_cache: dict[tuple[str, str], tuple] = {}

                for i, row in loc_df.iterrows():
                    target_date_obj = _to_date(row["target_date"])
                    lead_time_h = lead_times[i]
                    pred = preds[i]
                    bucket = _lead_time_bucket(lead_time_h)

                    # ACI correct sui bound CI (se warm). Drop-in trasparente:
                    # in cold start apply_aci_correction restituisce i bound CQR
                    # immutati + source="cqr_static", nessun effetto visibile.
                    if not dry_run:
                        aci_corrected = 0
                        aci_skipped = 0
                        for target in TARGETS:
                            key = (target, bucket)
                            if key not in aci_cache:
                                aci_cache[key] = get_aci_pair(db, target, bucket)
                            aci_80, aci_90 = aci_cache[key]
                            new_lo80, new_hi80, new_lo90, new_hi90, source = apply_aci_correction(
                                pred[target]["ci80_lo"], pred[target]["ci80_hi"],
                                pred[target]["ci90_lo"], pred[target]["ci90_hi"],
                                aci_80, aci_90,
                            )
                            if source == "aci":
                                aci_corrected += 1
                            else:
                                aci_skipped += 1
                            pred[target]["ci80_lo"] = new_lo80
                            pred[target]["ci80_hi"] = new_hi80
                            pred[target]["ci90_lo"] = new_lo90
                            pred[target]["ci90_hi"] = new_hi90
                        if aci_corrected and i == 0:
                            logger.debug(
                                f"[{location_id}] {target_date_obj} bucket={bucket}: "
                                f"ACI applicato a {aci_corrected}/{aci_corrected + aci_skipped} target"
                            )

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

                    nwp_comparison = get_nwp_model_comparison(
                        db, location_id, str(target_date_obj),
                    )

                    weather_code = get_daily_weather_code(
                        db, location_id, str(target_date_obj),
                    )

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
                    db.upsert_benchmark_forecasts([
                        {
                            "source":      cmp["source"],
                            "location_id": location_id,
                            "target_date": target_date_obj,
                            "lead_time_h": lead_time_h,
                            "tmin_c":      cmp["tmin_c"],
                            "tmax_c":      cmp["tmax_c"],
                            "precip_mm":   cmp["precip_mm"],
                        }
                        for cmp in nwp_comparison
                    ])
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

        if not dry_run:
            logger.info(f"JSON scritti: {[str(p) for p in json_paths]}")
        stats.rows = len(json_paths)
        stats.summary = f"{len(json_paths)} JSON scritti"


if __name__ == "__main__":
    app()
