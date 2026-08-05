"""Job CLI: review giornaliero — obs ieri + backfill + ACI + skill-history + monitor + train/skill condizionali.

Gira 1×/giorno alle 06:00 UTC, dopo che SIR ha pubblicato i dati validati di ieri.
Risponde a: "com'è andata ieri e il modello è ancora calibrato?"

Passi in sequenza:
  1. Ingestion obs ieri: SIR CSV delta + OM historical + OM multilead + Netatmo daily + QC
  2. Backfill obs su predictions passate (ts_valid <= ieri)
  3. Backfill obs su benchmark_forecasts passati
  4. Ricalcolo ACI da tutta la history
  5. Skill-history append (ieri) + dump skill_history.json
  6. Monitor coverage ACI (30gg rolling)
  7. [condizionale] train_all() se artefatti > TRAIN_INTERVAL_DAYS giorni
  8. [stesso gate] skill curve → skill.json

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
import yaml
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
from guazza.models import FEATURE_COLS, train_all, train_lgbm
from guazza.monitor import check_and_log, compute_coverage, update_aci_from_history
from guazza.netatmo_daily import aggregate_netatmo_daily
from guazza.qc import compute_quality_flags
from guazza.skill_history import DEFAULT_DUMP_PATH, append_one, atomic_write_json, dump_payload
from guazza.storage import DuckDBClient
from guazza.weights import load_configs

# ── Costanti ──────────────────────────────────────────────────────────────────

TRAIN_INTERVAL_DAYS: int = 7  # riallenare se artefatti più vecchi di N giorni

_MODEL_DIR_DEFAULT = Path(os.environ.get("MODEL_DIR", "/var/lib/guazza/models"))

# Variabili per la skill curve (identico a skill.py)
LEADS = [0, 24, 48, 72, 96, 120, 144, 168]
MIN_SAMPLES_PER_LEAD = 5
_SKILL_VARS = ["tmin_c", "tmax_c"]


def _curve_for(df: pd.DataFrame, var: str) -> list[dict[str, object]]:
    """Calcola la curva skill MAE per un singolo target su tutti i lead.

    Args:
        df: DataFrame con colonne lead_time_h, pred_{var}, nwp_{var}_mean, prim_{var}.
        var: nome target, es. "tmax_c".
    """
    nwp_col = f"nwp_{var.replace('_c', '')}_mean"  # es. nwp_tmax_mean
    pred_col = f"pred_{var}"
    prim_col = f"prim_{var}"
    points: list[dict[str, object]] = []
    for lead in LEADS:
        g = df[df["lead_time_h"] == lead].dropna(subset=[pred_col, nwp_col, prim_col])
        n = len(g)
        if n < MIN_SAMPLES_PER_LEAD:
            points.append({"lead_h": lead, "n": n, "mae_nwp": None, "mae_ml": None, "skill_pct": None})
            continue
        mae_nwp = float((g[nwp_col] - g[prim_col]).abs().mean())
        mae_ml = float((g[pred_col] - g[prim_col]).abs().mean())
        skill = (1 - mae_ml / mae_nwp) * 100 if mae_nwp else None
        points.append({
            "lead_h": lead, "n": n,
            "mae_nwp": round(mae_nwp, 3),
            "mae_ml": round(mae_ml, 3),
            "skill_pct": round(skill, 1) if skill is not None else None,
        })
    return points

app = typer.Typer(
    help="Review giornaliero: obs ieri + backfill + ACI + skill-history + monitor + train condizionale.",
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


def _primary_stations(config_dir: Path) -> dict[str, str]:
    data = yaml.safe_load((config_dir / "locations.yaml").read_text())
    return {
        loc_id: spec["sir_station_id"]
        for loc_id, spec in data["locations"].items()
        if spec.get("sir_station_id")
    }


def _run_skill_curve(
    db_path: Path,
    output_dir: Path,
    config_dir: Path,
    model_dir: Path,
    window_start: str = "2025-10-15",
    embargo_days: int = 7,
) -> None:
    """Calcola la curva di skill Guazza vs NWP e scrive skill.json."""
    stations = _primary_stations(config_dir)

    with DuckDBClient(db_path=db_path, read_only=True) as db_client:
        assert db_client._conn is not None
        df = db_client._conn.execute("SELECT * FROM features_daily").df()
        df["location_id"] = df["location_id"].astype("category")
        df["target_date"] = pd.to_datetime(df["target_date"]).dt.date
        df = df.sort_values("target_date").reset_index(drop=True)

        values = ", ".join(f"('{loc}','{st}')" for loc, st in stations.items())
        primary = db_client._conn.execute(f"""
            WITH st(location_id, station_id) AS (VALUES {values})
            SELECT st.location_id, o.ts::date AS target_date,
                   o.tmin_c AS prim_tmin_c, o.tmax_c AS prim_tmax_c
            FROM observations o JOIN st ON o.station_id = st.station_id
            WHERE o.source = 'sir_toscana' AND o.granularity = 'daily'
        """).df()
        primary["target_date"] = pd.to_datetime(primary["target_date"]).dt.date

    win_start = pd.to_datetime(window_start).date()
    cutoff = (pd.Timestamp(win_start) - pd.Timedelta(days=embargo_days)).date()

    train_df = df[df["target_date"] <= cutoff]
    models: dict[str, Any] = {}
    for var in _SKILL_VARS:
        col = f"target_{var}"
        mask = train_df[col].notna()
        models[var] = train_lgbm(train_df.loc[mask, FEATURE_COLS], train_df.loc[mask, col], 0.50)
    logger.info(f"skill curve train q=0.50: {len(train_df)} righe ≤ {cutoff} | test ≥ {win_start}")

    test = df[df["target_date"] >= win_start].copy()
    for var in _SKILL_VARS:
        test[f"pred_{var}"] = models[var].predict(test[FEATURE_COLS])
    test = test.merge(primary, on=["location_id", "target_date"], how="left")

    evaluated = primary[primary["target_date"] >= win_start]["target_date"]
    window_end = max(evaluated) if len(evaluated) else win_start

    locations_out: dict[str, Any] = {}
    for loc_id in sorted(stations):
        loc_test = test[test["location_id"] == loc_id].copy()
        # Rinomina colonne per far combaciare lo schema atteso da _curve_for:
        #   nwp_{var}_mean → già ok (_TARGET_NWP_MEAN)
        #   prim_{tmin,tmax}_c → già ok dal join con primary
        for var in _SKILL_VARS:
            prim_col_src = f"prim_{var.replace('_c', '')}_c"
            loc_test[f"prim_{var}"] = loc_test[prim_col_src]
        curves: dict[str, list[dict[str, object]]] = {}
        for var in _SKILL_VARS:
            curves[var] = _curve_for(loc_test, var)
        locations_out[loc_id] = {"sir_station_id": stations[loc_id], **curves}

    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "ground_truth": "sir_primary",
        "window_start": win_start.isoformat(),
        "window_end": window_end.isoformat(),
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
    """Review giornaliero: obs ieri + backfill + ACI + skill-history + monitor + train condizionale."""
    if not date_str:
        date_str = (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")

    with job_run("job_review") as stats:
        locations, stations = load_configs(config_dir)

        with DuckDBClient(db_path=db_path) as db:
            db.init_schema()

            # ── 1. Ingestion obs ieri ────────────────────────────────────────
            if not dry_run:
                sir_total = _ingest_sir_historical_range(
                    db, locations, stations, date_str, date_str
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
                    start_date=date_str,
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
                    start_date=date_str,
                    end_date=om_end_date,
                    on_records=_on_ml,
                )
                logger.info(f"review Open-Meteo multilead: {ml_total} record")

                date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
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
                assert db._conn is not None
                n_sh = append_one(db._conn, date_obj)
                logger.info(f"review skill-history: {n_sh} righe upsert ({date_obj})")
                payload = dump_payload(db._conn)
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
                    logger.warning(f"review monitor: drift su {n_alerts} combinazioni — healthchecks /fail")

        # ── 7-8. Train + skill condizionali (fuori dal writer DuckDB) ────────
        # DuckDB è single-writer: il context manager writer è già chiuso.
        # train_all apre read_only internamente.
        if not dry_run and _should_train(model_dir, force_train):
            logger.info(f"review: train condizionale (artefatti > {TRAIN_INTERVAL_DAYS}gg)")
            with DuckDBClient(db_path=db_path, read_only=True) as db_ro:
                artifacts = train_all(db_ro, model_dir=model_dir, cal_days=90)
            logger.info(f"review train completato: n_train={artifacts.n_train} n_cal={artifacts.n_cal}")
            _run_skill_curve(db_path, output_dir, config_dir, model_dir)

        stats.summary = f"date={date_str} dry_run={dry_run} force_train={force_train}"


if __name__ == "__main__":
    app()
