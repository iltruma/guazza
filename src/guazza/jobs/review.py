"""Job CLI: review giornaliero — obs [ieri-7, ieri] + backfill + ACI + skill-history + monitor + skill + train condizionale.

Gira 1×/giorno alle 06:00 UTC, dopo che SIR ha pubblicato i dati validati di ieri.
Risponde a: "com'è andata ieri e il modello è ancora calibrato?"

Passi in sequenza:
  1. Ingestion finestra [ieri-7, ieri]: SIR CSV delta + OM historical + OM multilead + Netatmo daily + QC
     (finestra di recupero: un run perso viene auto-riparato dal successivo; il costo
     rete è invariato — il CSV SIR restituisce comunque tutto lo storico)
  2. Backfill obs su predictions passate (ts_valid <= ieri)
  3. Backfill obs su benchmark_forecasts passati
  4. Ricalcolo ACI da tutta la history
  5. Skill-history append (ieri) + dump skill_history.json
  6. Monitor coverage ACI (30gg rolling)
  7. [condizionale] train_all() se artefatti > TRAIN_INTERVAL_DAYS giorni
  8. skill curve → skill.json (ogni giorno, non condizionale al train)

Uso:
    uv run python -m guazza.jobs.review run
    uv run python -m guazza.jobs.review run --dry-run
    uv run python -m guazza.jobs.review run --force-train
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.features import build_features_daily
from guazza.fetch_openmeteo import fetch_openmeteo_historical_batch, fetch_openmeteo_multilead_batch
from guazza.jobs._common import (
    CONFIG_DIR_OPTION,
    DB_OPTION,
    OUTPUT_DIR_OPTION,
    job_run,
    ping_monitor_alert,
)
from guazza.jobs.ingest import (
    _ingest_sir_historical_range,
)
from guazza.models import RAIN_THRESHOLD_MM, train_all
from guazza.monitor import check_and_log, compute_coverage, update_aci_from_history
from guazza.netatmo_daily import aggregate_netatmo_daily
from guazza.qc import compute_quality_flags
from guazza.skill_history import DEFAULT_DUMP_PATH, append_one, atomic_write_json, dump_payload
from guazza.storage import DuckDBClient
from guazza.weights import load_configs, primary_stations

# ── Costanti ──────────────────────────────────────────────────────────────────

TRAIN_INTERVAL_DAYS: int = 7  # riallenare se artefatti più vecchi di N giorni

_MODEL_DIR_DEFAULT = Path(os.environ.get("MODEL_DIR", "/var/lib/guazza/models"))

# Variabili per la skill curve (usate in _run_skill_curve)
LEADS = [0, 24, 48, 72, 96, 120, 144, 168]
MIN_SAMPLES_PER_LEAD = 2
_SKILL_VARS = ["tmin_c", "tmax_c"]


def _curve_for(df: pd.DataFrame, var: str) -> list[dict[str, object]]:
    """Calcola la curva skill MAE per un singolo target su tutti i lead.

    Args:
        df: DataFrame con colonne lead_time_h, {stem}_p50, nwp_{stem}_mean, {stem}_obs
            (stem = var senza suffisso _c, es. tmax).
        var: nome target, es. "tmax_c".
    """
    stem = var.replace("_c", "")
    nwp_col = f"nwp_{stem}_mean"  # es. nwp_tmax_mean — consensus NWP da features_daily
    pred_col = f"{stem}_p50"      # es. tmax_p50 — p50 di produzione
    obs_col = f"{stem}_obs"       # es. tmax_obs — obs SIR pesate backfillate
    points: list[dict[str, object]] = []
    for lead in LEADS:
        g = df[df["lead_time_h"] == lead].dropna(subset=[pred_col, nwp_col, obs_col])
        n = len(g)
        if n < MIN_SAMPLES_PER_LEAD:
            points.append({"lead_h": lead, "n": n, "mae_nwp": None, "mae_ml": None, "skill_pct": None})
            continue
        mae_nwp = float((g[nwp_col] - g[obs_col]).abs().mean())
        mae_ml = float((g[pred_col] - g[obs_col]).abs().mean())
        skill = (1 - mae_ml / mae_nwp) * 100 if mae_nwp else None
        points.append({
            "lead_h": lead, "n": n,
            "mae_nwp": round(mae_nwp, 3),
            "mae_ml": round(mae_ml, 3),
            "skill_pct": round(skill, 1) if skill is not None else None,
        })
    return points


def _coverage_for(df: pd.DataFrame, var: str) -> list[dict[str, object]]:
    """Copertura empirica CI80/CI90 per lead dalle predictions di produzione.

    Le predictions contengono gli intervalli CQR+ACI scritti in produzione
    (forecast.py applica apply_aci_correction prima dell'upsert): questa è la
    copertura del sistema reale, sulla stessa finestra della skill curve.
    """
    stem = var.replace("_c", "")
    obs_col = f"{stem}_obs"
    points: list[dict[str, object]] = []
    for lead in LEADS:
        cols = [obs_col, f"{stem}_ci80_lo", f"{stem}_ci80_hi",
                f"{stem}_ci90_lo", f"{stem}_ci90_hi"]
        g = df[df["lead_time_h"] == lead].dropna(subset=cols)
        n = len(g)
        if n < MIN_SAMPLES_PER_LEAD:
            points.append({"lead_h": lead, "n": n, "cov80": None, "cov90": None})
            continue
        cov80 = float(((g[f"{stem}_ci80_lo"] <= g[obs_col])
                       & (g[obs_col] <= g[f"{stem}_ci80_hi"])).mean())
        cov90 = float(((g[f"{stem}_ci90_lo"] <= g[obs_col])
                       & (g[obs_col] <= g[f"{stem}_ci90_hi"])).mean())
        points.append({
            "lead_h": lead, "n": n,
            "cov80": round(cov80, 3),
            "cov90": round(cov90, 3),
        })
    return points


def _rain_prob_for(df: pd.DataFrame) -> list[dict[str, object]]:
    """Brier score della P(pioggia): Guazza (prob_rain di produzione) vs NWP-consensus.

    Evento: precip_obs > RAIN_THRESHOLD_MM (obs SIR pesate backfillate).
    Baseline NWP: frazione binaria nwp_precip_mean > soglia — stessa baseline
    usata da walk_forward_cv (cv.py). Si popola dal deploy: le predictions
    precedenti non hanno rain_prob (colonna nuova).
    """
    points: list[dict[str, object]] = []
    for lead in LEADS:
        g = df[df["lead_time_h"] == lead].dropna(
            subset=["rain_prob", "precip_obs", "nwp_precip_mean"]
        )
        n = len(g)
        if n < MIN_SAMPLES_PER_LEAD:
            points.append({"lead_h": lead, "n": n, "brier_g": None, "brier_n": None,
                           "p_wet_g": None, "p_dry_g": None})
            continue
        event = (g["precip_obs"] > RAIN_THRESHOLD_MM).astype(float)
        nwp_prob = (g["nwp_precip_mean"] > RAIN_THRESHOLD_MM).astype(float)
        wet = g[event == 1.0]
        dry = g[event == 0.0]
        points.append({
            "lead_h": lead, "n": n,
            "brier_g": round(float(((g["rain_prob"] - event) ** 2).mean()), 4),
            "brier_n": round(float(((nwp_prob - event) ** 2).mean()), 4),
            "p_wet_g": round(float(wet["rain_prob"].mean()), 3) if len(wet) else None,
            "p_dry_g": round(float(dry["rain_prob"].mean()), 3) if len(dry) else None,
        })
    return points

app = typer.Typer(
    help="Review giornaliero: ingest [ieri-7, ieri] + backfill + ACI + skill-history + monitor + skill + train condizionale.",
    no_args_is_help=True,
)


@app.callback()
def _callback() -> None:
    setup_logging()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _should_train(model_dir: Path, force_train: bool) -> bool:
    """True se gli artefatti sono assenti o più vecchi di TRAIN_INTERVAL_DAYS."""
    if force_train:
        return True
    manifest = model_dir / "artifacts.json"
    if not manifest.exists():
        return True
    try:
        data = json.loads(manifest.read_text())
        trained_at = datetime.fromisoformat(data["trained_at"])
        age_days = (datetime.now(tz=UTC) - trained_at).days
        return age_days >= TRAIN_INTERVAL_DAYS
    except Exception:
        return True  # se non riesce a leggere, riallenare per sicurezza



def _run_skill_curve(
    db_path: Path,
    output_dir: Path,
    config_dir: Path,
    embargo_days: int = 7,
    window_days: int = 90,
) -> None:
    """Calcola la curva skill e la copertura CI per-lead dalle predictions reali di produzione e scrive skill.json.

    Ground truth: obs SIR pesate (obs_weighted_daily) backfillate sulle predictions
    dal review (backfill_prediction_obs). Guazza = p50 di produzione (modello
    riallenato settimanalmente); NWP = consensus dei 4 modelli alla stessa lead da
    features_daily (stessa aggregazione usata in training). La copertura CI80/CI90
    usa gli intervalli CQR+ACI scritti in produzione (sezione "coverage" del payload).
    La sezione "rain_prob" riporta il Brier score della P(pioggia) (prob_rain di
    produzione vs baseline NWP binaria, soglia RAIN_THRESHOLD_MM).

    Finestra mobile [oggi - embargo - window, oggi - embargo]: l'embargo esclude
    le osservazioni più recenti non ancora backfillate/validate.

    Sostituisce la vecchia curva con modello congelato su split fisso (2025-10-15):
    quel modello non era quello in produzione e la finestra di test cresceva
    all'infinito. Ora si misura il sistema reale su una finestra recente.
    """
    stations = primary_stations(config_dir)

    end = (datetime.now(tz=UTC) - timedelta(days=embargo_days)).date()
    start = end - timedelta(days=window_days)
    start_iso, end_iso = start.isoformat(), end.isoformat()
    logger.info(f"skill curve finestra {start_iso} → {end_iso} (embargo {embargo_days}gg)")

    with DuckDBClient(db_path=db_path, read_only=True) as db_client:
        # Predictions di produzione: ultima model_version per (location, giorno, lead)
        preds = db_client.execute("""
            SELECT location_id, ts_valid::DATE AS target_date, lead_time_h,
                   tmin_p50, tmax_p50, tmin_obs, tmax_obs,
                   tmin_ci80_lo, tmin_ci80_hi, tmin_ci90_lo, tmin_ci90_hi,
                   tmax_ci80_lo, tmax_ci80_hi, tmax_ci90_lo, tmax_ci90_hi,
                   precip_obs, rain_prob
            FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY location_id, ts_valid::DATE, lead_time_h
                    ORDER BY model_version DESC
                ) AS _rn
                FROM predictions
                WHERE ts_valid::DATE BETWEEN ? AND ?
            ) s
            WHERE s._rn = 1
        """, [start_iso, end_iso]).df()
        preds["target_date"] = pd.to_datetime(preds["target_date"]).dt.date

        # NWP consensus per lead (stessa aggregazione delle features del modello)
        nwp = db_client.execute("""
            SELECT location_id, target_date, lead_time_h,
                   nwp_tmin_mean, nwp_tmax_mean, nwp_precip_mean
            FROM features_daily
            WHERE target_date BETWEEN ? AND ?
        """, [start_iso, end_iso]).df()
        nwp["target_date"] = pd.to_datetime(nwp["target_date"]).dt.date

    test = preds.merge(nwp, on=["location_id", "target_date", "lead_time_h"], how="left")

    locations_out: dict[str, Any] = {}
    for loc_id in sorted(stations):
        loc_test = test[test["location_id"] == loc_id].copy()
        curves: dict[str, list[dict[str, object]]] = {}
        coverage: dict[str, list[dict[str, object]]] = {}
        for var in _SKILL_VARS:
            curves[var] = _curve_for(loc_test, var)
            coverage[var] = _coverage_for(loc_test, var)
        locations_out[loc_id] = {
            "sir_station_id": stations[loc_id], **curves, "coverage": coverage,
            "rain_prob": _rain_prob_for(loc_test),
        }

    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "ground_truth": "sir_weighted",
        "window_start": start_iso,
        "window_end": end_iso,
        "embargo_days": embargo_days,
        "leads_h": LEADS,
        "min_samples_per_lead": MIN_SAMPLES_PER_LEAD,
        "locations": locations_out,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / "skill.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    tmp.replace(out)
    logger.info(f"review skill.json scritto: {out} ({len(locations_out)} location)")


# ── Comando principale ────────────────────────────────────────────────────────

@app.command("run")
def cmd_run(
    db_path: Path = DB_OPTION,
    config_dir: Path = CONFIG_DIR_OPTION,
    output_dir: Path = OUTPUT_DIR_OPTION,
    model_dir: Path = typer.Option(_MODEL_DIR_DEFAULT, "--model-dir", help="Directory artefatti modello"),
    date_str: str = typer.Option("", "--date", help="Giorno da processare YYYY-MM-DD (default: ieri)"),
    skill_output: Path = typer.Option(DEFAULT_DUMP_PATH, "--skill-output", help="Path skill_history.json"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Salta scritture, esegue solo monitor"),
    force_train: bool = typer.Option(False, "--force-train", help="Forza train anche se artefatti recenti"),
) -> None:
    """Review giornaliero: ingest [ieri-7, ieri] + backfill + ACI + skill-history + monitor + skill + train condizionale."""
    if not date_str:
        date_str = (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    ingest_start = (date_obj - timedelta(days=7)).isoformat()

    with job_run("job_review") as stats:
        locations, stations = load_configs(config_dir)

        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            # ── 1. Ingestion finestra [ieri-7, ieri] ─────────────────────────
            if not dry_run:
                sir_total = _ingest_sir_historical_range(
                    db, locations, stations, ingest_start, date_str
                )
                logger.info(f"review SIR: {sir_total} record")

                # OM historical: lead=0 retroattivo
                om_hist_total = 0

                def _on_hist(records: list[dict[str, Any]]) -> None:
                    nonlocal om_hist_total
                    om_hist_total += db.upsert_forecasts(records)

                # L'archivio Historical Forecast API arriva fino a 2 giorni fa
                om_end_date = min(
                    datetime.fromisoformat(date_str).date(),
                    (datetime.now(tz=UTC) - timedelta(days=2)).date(),
                ).isoformat()

                fetch_openmeteo_historical_batch(
                    locations=locations,
                    start_date=ingest_start,
                    end_date=om_end_date,
                    on_records=_on_hist,
                )
                logger.info(f"review Open-Meteo historical: {om_hist_total} record")

                # OM multilead: cosa i modelli prevedevano per ieri
                ml_total = 0

                def _on_ml(records: list[dict[str, Any]]) -> None:
                    nonlocal ml_total
                    ml_total += db.upsert_forecasts(records)

                fetch_openmeteo_multilead_batch(
                    locations=locations,
                    start_date=ingest_start,
                    end_date=om_end_date,
                    on_records=_on_ml,
                )
                logger.info(f"review Open-Meteo multilead: {ml_total} record")

                nd = aggregate_netatmo_daily(db, target_day=date_obj, min_samples=6)
                logger.info(f"review Netatmo: {nd['rows']} record")

                qc = compute_quality_flags(db)
                logger.info(f"review QC: {qc['total']} flag")

                # F3: build features dopo QC così il train che segue trova dati aggiornati
                n_feat = build_features_daily(db)
                logger.info(f"review features: {n_feat} righe in features_daily")

            # ── 2-3. Backfill obs su predictions e benchmark ─────────────────
            if not dry_run:
                n_backfilled = db.backfill_prediction_obs()
                if n_backfilled:
                    logger.info(f"review obs backfill: {n_backfilled} predictions")
                n_bench = db.backfill_benchmark_obs()
                if n_bench:
                    logger.info(f"review bench backfill: {n_bench} benchmark")

            # ── 4. Ricalcolo ACI da tutta la history ─────────────────────────
            if not dry_run:
                n_aci = update_aci_from_history(db)
                if n_aci:
                    logger.info(f"review ACI: {n_aci} coppie aggiornate")

            # ── 5. Skill-history append (ieri) + dump ─────────────────────────
            if not dry_run:
                n_sh = append_one(db, date_obj)
                logger.info(f"review skill-history: {n_sh} righe upsert ({date_obj})")
                payload = dump_payload(db)
                atomic_write_json(skill_output, payload)
                logger.info(f"review skill-history dump: {skill_output}")

            # ── 6. Monitor coverage ACI (gira anche in dry-run) ──────────────
            coverage_results = compute_coverage(db)
            if not coverage_results:
                logger.warning("review monitor: nessuna prediction con actual negli ultimi 30gg")
            else:
                n_alerts = check_and_log(coverage_results)
                if n_alerts > 0 and not dry_run:
                    ping_monitor_alert()
                    logger.warning(f"review monitor: drift su {n_alerts} combinazioni — kuma push status=down")

        # ── 7-8. Train condizionale + skill giornaliera (fuori dal writer DuckDB) ──
        # DuckDB è single-writer: il context manager writer è già chiuso.
        # train_all apre read_only internamente.
        if not dry_run and _should_train(model_dir, force_train):
            logger.info(f"review: train condizionale (artefatti > {TRAIN_INTERVAL_DAYS}gg)")
            with DuckDBClient(db_path=db_path, read_only=True) as db_ro:
                artifacts = train_all(db_ro, model_dir=model_dir, cal_days=90)
            logger.info(f"review train completato: n_train={artifacts.n_train} n_cal={artifacts.n_cal}")

        if not dry_run:
            _run_skill_curve(db_path, output_dir, config_dir)

        stats.summary = f"window={ingest_start}..{date_str} dry_run={dry_run} force_train={force_train}"


if __name__ == "__main__":
    app()
