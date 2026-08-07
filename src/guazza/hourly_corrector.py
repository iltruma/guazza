"""Correttore orario: LightGBM che corregge la forma della curva oraria del profilo daily.

Il profilo daily oggi è la shape NWP oraria rescalata sugli anchor ML daily
(tmin_p50, tmax_p50). Questo modulo addestra un LightGBM regression che impara il
delta sistematico Δ(h) = obs_median(h) − shape_obs(h) tra la curva osservata e la
shape NWP ancorata alle osservazioni, dato hour/month/location/shape_norm/wc/precip/wind/humidity.

Pipeline:
  build_delta_dataset(db, locations) → dataset con target delta e split train/val/test
  train_corrector(dataset, out_path) → Booster salvato se improvement >= soglia
  load_corrector(model_dir) → Booster oppure None se non disponibile
  predict_delta(corrector, features_row) → float delta oppure None su errore
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.db_queries import _modal_weather_code
from guazza.jobs._common import CONFIG_DIR_OPTION, DB_OPTION
from guazza.storage import DuckDBClient
from guazza.weights import load_configs

# ── Costanti ──────────────────────────────────────────────────────────────────

CORRECTOR_FILENAME: str = "hourly_corrector.lgb"
MIN_SAMPLES_PER_SLOT: int = 3
MIN_DAYS_PER_LOCATION: int = 60
EMBARGO_DAYS: int = 7
TEST_DAYS: int = 30
MIN_RMSE_IMPROVEMENT_PCT: float = 15.0
DELTA_CLAMP_C: float = 10.0

EXCLUDE_FLAGS: set[str] = {
    "spike_realtime",
    "stall_sensor",
    "bias_solar",
    "range_precip_high",
}

FEATURES: list[str] = [
    "hour",
    "month",
    "location_id",
    "shape_norm",
    "wc",
    "precip_flag",
    "wind_ms",
    "humidity_pct",
]

_MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/var/lib/guazza/models"))
_MODEL_DIR_OPTION = typer.Option(_MODEL_DIR, "--model-dir", help="Directory artefatti modello")

# ── Core API ──────────────────────────────────────────────────────────────────


def load_corrector(model_dir: Path) -> Any | None:
    """Carica il correttore orario da model_dir/CORRECTOR_FILENAME.

    Returns:
        lightgbm.Booster oppure None se il file non esiste o il caricamento fallisce.
    """
    path = model_dir / CORRECTOR_FILENAME
    if not path.exists():
        return None
    try:
        return lgb.Booster(model_str=path.read_text())
    except Exception:
        logger.warning(f"load_corrector: impossibile caricare {path}")
        return None


def predict_delta(corrector: Any, features_row: dict[str, Any]) -> float | None:
    """Predice il delta di correzione oraria per una singola riga di feature.

    Args:
        corrector: oggetto con metodo .predict(df) — tipicamente lgb.Booster.
        features_row: dict con chiavi in FEATURES (almeno); location_id deve essere str.

    Returns:
        float delta clampato a ±DELTA_CLAMP_C, oppure None su qualsiasi eccezione.
    """
    try:
        df = pd.DataFrame([{f: features_row.get(f) for f in FEATURES}])
        df["location_id"] = df["location_id"].astype("category")
        raw = corrector.predict(df)
        val = float(raw[0]) if hasattr(raw, "__getitem__") else float(raw)
        return float(np.clip(val, -DELTA_CLAMP_C, DELTA_CLAMP_C))
    except Exception:
        return None


def build_delta_dataset(
    db: DuckDBClient,
    locations: dict[str, Any],
    max_date: date | None = None,
    min_days: int = MIN_DAYS_PER_LOCATION,
) -> pd.DataFrame | None:
    """Costruisce il dataset di addestramento per il correttore orario.

    Per ogni location: shape NWP normalizzata per giorno + obs realtime mediane per
    slot orario. Target Δ(h) = obs_median(h) − shape_obs(h) (residuo di shape).

    Args:
        db:         connessione DuckDB attiva.
        locations:  dict location_id → spec (come da load_configs).
        max_date:   tetto superiore date (default: oggi).
        min_days:   giorni minimi per location per includere nel dataset (default: MIN_DAYS_PER_LOCATION).

    Returns:
        DataFrame con colonne FEATURES + ["delta", "date", "split"], oppure None se
        dati insufficienti per qualsiasi location presente nel dataset.
    """
    if max_date is None:
        max_date = date.today()

    all_rows: list[dict[str, Any]] = []

    for loc_id in locations:
        loc_rows, n_days = _build_location_rows(db, str(loc_id), max_date)
        if n_days < min_days:
            logger.debug(f"[{loc_id}] {n_days} giorni < {min_days} — dati insufficienti")
            return None
        all_rows.extend(loc_rows)

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"]).dt.date

    # Split cronologico globale (tutte le location condividono lo stesso range di date)
    dates = sorted(df["date"].unique())
    if len(dates) < TEST_DAYS + EMBARGO_DAYS + 1:
        return None

    test_start = dates[-TEST_DAYS]
    val_start_idx = max(0, len(dates) - TEST_DAYS - EMBARGO_DAYS)
    val_start = dates[val_start_idx]

    def _split(d: date) -> str:
        if d >= test_start:
            return "test"
        if d >= val_start:
            return "val"
        return "train"

    df["split"] = df["date"].map(_split)
    return df


def _build_location_rows(
    db: DuckDBClient,
    loc_id: str,
    max_date: date,
) -> tuple[list[dict[str, Any]], int]:
    """Costruisce le righe delta per una singola location.

    Returns:
        (rows, n_unique_days_with_rows)
    """
    # 1. NWP ensemble mean per (local_date, hour) — run più recente per (source, ts_valid)
    nwp_df = db.execute("""
        SELECT
            CAST(local_ts AS DATE) AS local_date,
            HOUR(local_ts)        AS hour,
            AVG(temp_c)           AS temp_mean,
            AVG(humidity_pct)     AS humidity_mean,
            AVG(COALESCE(precip_mm, 0.0)) AS precip_mean,
            AVG(wind_speed_ms)    AS wind_mean
        FROM (
            SELECT
                ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS local_ts,
                temp_c, humidity_pct, precip_mm, wind_speed_ms
            FROM forecasts
            WHERE location_id = ?
              AND CAST(ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE) <= ?
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY source, ts_valid
                ORDER BY ts_run DESC
            ) = 1
        ) latest
        WHERE temp_c IS NOT NULL
        GROUP BY local_date, hour
        ORDER BY local_date, hour
    """, [loc_id, str(max_date)]).df()

    if nwp_df.empty:
        return [], 0

    nwp_df["local_date"] = pd.to_datetime(nwp_df["local_date"]).dt.date

    # Weather_code modale per (local_date, hour) — stesso pattern output.py
    wc_raw = db.execute("""
        SELECT
            CAST(local_ts AS DATE) AS local_date,
            HOUR(local_ts)        AS local_hour,
            weather_code
        FROM (
            SELECT
                ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS local_ts,
                weather_code
            FROM forecasts
            WHERE location_id = ?
              AND weather_code IS NOT NULL
              AND CAST(ts_valid AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE) <= ?
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY source, ts_valid
                ORDER BY ts_run DESC
            ) = 1
        ) latest
    """, [loc_id, str(max_date)]).fetchall()

    wc_codes: dict[tuple[Any, int], list[int]] = {}
    for ldate, lhour, wc in wc_raw:
        key = (ldate, int(lhour))
        wc_codes.setdefault(key, []).append(int(wc))
    wc_modal: dict[tuple[Any, int], int | None] = {
        k: _modal_weather_code(v) for k, v in wc_codes.items()
    }

    # 2. Obs realtime pesate per (local_date, hour) con ≥ MIN_SAMPLES_PER_SLOT campioni.
    #    Media pesata via station_weights (stesso schema di obs_weighted_daily e current JSON)
    #    per coerenza con il ground truth daily usato dal modello ML.
    #    Esclude righe con flag_type in EXCLUDE_FLAGS
    exclude_sql = ", ".join(f"'{f}'" for f in EXCLUDE_FLAGS)
    obs_df = db.execute(f"""
        SELECT
            CAST(o.ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE) AS local_date,
            HOUR(o.ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome')         AS hour,
            SUM(o.temp_c * sw.weight)
                / NULLIF(SUM(CASE WHEN o.temp_c IS NOT NULL THEN sw.weight ELSE 0 END), 0)
                AS obs_median,
            COUNT(*)       AS n_samples
        FROM observations o
        JOIN station_weights sw
          ON o.station_id = sw.station_id AND sw.location_id = ?
        WHERE o.granularity = 'realtime'
          AND o.temp_c IS NOT NULL
          AND CAST(o.ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE) <= ?
          AND NOT EXISTS (
              SELECT 1 FROM quality_flags qf
              WHERE qf.source = o.source
                AND qf.station_id = o.station_id
                AND qf.ts = o.ts
                AND qf.granularity = o.granularity
                AND qf.flag_type IN ({exclude_sql})
          )
        GROUP BY local_date, hour
        HAVING COUNT(*) >= {MIN_SAMPLES_PER_SLOT}
    """, [loc_id, str(max_date)]).df()

    if obs_df.empty:
        return [], 0

    obs_df["local_date"] = pd.to_datetime(obs_df["local_date"]).dt.date

    # 3. Per ogni giorno: normalizza shape NWP, calcola delta vs obs rescalata
    rows: list[dict[str, Any]] = []
    unique_days: set[date] = set()

    nwp_by_date: dict[Any, pd.DataFrame] = {
        d: g.reset_index(drop=True)
        for d, g in nwp_df.groupby("local_date")
    }
    obs_by_date: dict[Any, pd.DataFrame] = {
        d: g.reset_index(drop=True)
        for d, g in obs_df.groupby("local_date")
    }

    for d, nwp_day in nwp_by_date.items():
        if len(nwp_day) < 2:
            continue
        obs_day = obs_by_date.get(d)
        if obs_day is None or len(obs_day) < 2:
            continue

        day_min = float(nwp_day["temp_mean"].min())
        day_max = float(nwp_day["temp_mean"].max())
        span = day_max - day_min
        if span <= 0:
            continue

        obs_day_min = float(obs_day["obs_median"].min())
        obs_day_max = float(obs_day["obs_median"].max())
        obs_span = obs_day_max - obs_day_min
        if obs_span <= 0:
            continue

        # Shape NWP normalizzata per ora
        nwp_shape: dict[int, float] = {
            int(r["hour"]): (float(r["temp_mean"]) - day_min) / span
            for _, r in nwp_day.iterrows()
        }

        # Obs per ora (solo ore con ≥ MIN_SAMPLES_PER_SLOT campioni già filtrate da SQL)
        obs_by_hour: dict[int, float] = {
            int(r["hour"]): float(r["obs_median"])
            for _, r in obs_day.iterrows()
        }

        # NWP feature per ora
        nwp_feat: dict[int, dict[str, Any]] = {
            int(r["hour"]): {
                "precip_mean": float(r["precip_mean"]),
                "wind_mean": float(r["wind_mean"]) if r["wind_mean"] is not None else None,
                "humidity_mean": float(r["humidity_mean"]) if r["humidity_mean"] is not None else None,
            }
            for _, r in nwp_day.iterrows()
        }

        month = d.month

        for h in sorted(set(nwp_shape) & set(obs_by_hour)):
            shape_norm = nwp_shape[h]
            obs_med = obs_by_hour[h]
            shape_obs = obs_day_min + shape_norm * obs_span
            delta = obs_med - shape_obs

            feat = nwp_feat[h]
            precip_flag = 1 if feat["precip_mean"] > 0.1 else 0
            wc_val = wc_modal.get((d, h))

            rows.append({
                "hour":         h,
                "month":        month,
                "location_id":  loc_id,
                "shape_norm":   shape_norm,
                "wc":           float(wc_val) if wc_val is not None else None,
                "precip_flag":  precip_flag,
                "wind_ms":      feat["wind_mean"],
                "humidity_pct": feat["humidity_mean"],
                "delta":        delta,
                "date":         d,
            })
            unique_days.add(d)

    return rows, len(unique_days)


def train_corrector(
    dataset: pd.DataFrame,
    out_path: Path,
    min_improvement_pct: float = MIN_RMSE_IMPROVEMENT_PCT,
) -> dict[str, float] | None:
    """Addestra il correttore orario e lo salva se supera la soglia di miglioramento.

    Args:
        dataset:              DataFrame da build_delta_dataset con colonna 'split'.
        out_path:             Path dove salvare il Booster (CORRECTOR_FILENAME).
        min_improvement_pct:  soglia minima % di miglioramento RMSE su test.

    Returns:
        Dict con metriche di valutazione, oppure None se improvement < soglia
        (il file NON viene scritto).
    """
    train = dataset[dataset["split"] == "train"].copy()
    val   = dataset[dataset["split"] == "val"].copy()
    test  = dataset[dataset["split"] == "test"].copy()

    X_train = train[FEATURES].copy()
    X_train["location_id"] = X_train["location_id"].astype("category")
    y_train = train["delta"]

    model = lgb.LGBMRegressor(
        objective="regression",
        num_leaves=31,
        learning_rate=0.05,
        n_estimators=500,
        random_state=42,
        n_jobs=-1,
        verbose=-1,
    )

    if not val.empty:
        X_val = val[FEATURES].copy()
        X_val["location_id"] = X_val["location_id"].astype("category")
        model.fit(
            X_train, y_train,
            categorical_feature=["location_id"],
            eval_set=[(X_val, val["delta"])],
            callbacks=[
                lgb.early_stopping(50, verbose=False),
                lgb.log_evaluation(0),
            ],
        )
    else:
        model.fit(X_train, y_train, categorical_feature=["location_id"])

    # Valutazione su test
    X_test = test[FEATURES].copy()
    X_test["location_id"] = X_test["location_id"].astype("category")
    preds = model.predict(X_test)
    y_test = test["delta"].to_numpy()

    rmse_model = float(np.sqrt(np.mean((y_test - preds) ** 2)))
    rmse_base  = float(np.sqrt(np.mean(y_test ** 2)))

    improvement_pct = (
        100.0 * (rmse_base - rmse_model) / rmse_base
        if rmse_base > 0 else 0.0
    )

    if improvement_pct < min_improvement_pct:
        logger.info(
            f"train_corrector: improvement {improvement_pct:.1f}% < soglia {min_improvement_pct:.1f}% — nessun salvataggio"
        )
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    model.booster_.save_model(str(out_path))

    metrics: dict[str, float] = {
        "rmse_base":       round(rmse_base, 4),
        "rmse_model":      round(rmse_model, 4),
        "improvement_pct": round(improvement_pct, 2),
        "n_train":         float(len(train)),
        "n_val":           float(len(val)),
        "n_test":          float(len(test)),
    }
    return metrics


def _per_hour_rmse(
    test_df: pd.DataFrame,
    preds: np.ndarray,
) -> dict[int, dict[str, float]]:
    """RMSE per ora: modello vs baseline (Δ=0) sul test set."""
    test_df = test_df.copy()
    test_df["pred"] = preds
    result: dict[int, dict[str, float]] = {}
    for h in range(24):
        g = test_df[test_df["hour"] == h]
        if g.empty:
            continue
        y = g["delta"].to_numpy()
        p = g["pred"].to_numpy()
        result[h] = {
            "rmse_base":  round(float(np.sqrt(np.mean(y**2))), 3),
            "rmse_model": round(float(np.sqrt(np.mean((y - p)**2))), 3),
        }
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

app = typer.Typer(
    help="Correttore orario: LightGBM sulla forma della curva oraria.",
    no_args_is_help=True,
)


@app.callback()
def _callback() -> None:
    setup_logging()


@app.command("train")
def cmd_train(
    db_path:    Path = DB_OPTION,
    config_dir: Path = CONFIG_DIR_OPTION,
    model_dir:  Path = _MODEL_DIR_OPTION,
    min_days:   int  = typer.Option(MIN_DAYS_PER_LOCATION, "--min-days", help="Giorni minimi per location"),
) -> None:
    """Addestra il correttore orario e salva in MODEL_DIR/hourly_corrector.lgb."""
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
        locations, _ = load_configs(config_dir)
        dataset = build_delta_dataset(db, locations, min_days=min_days)

    if dataset is None:
        logger.warning("train: dati insufficienti — correttore non addestrato")
        raise typer.Exit(0)

    out_path = model_dir / CORRECTOR_FILENAME
    metrics = train_corrector(dataset, out_path)

    if metrics is None:
        logger.warning("train: improvement sotto soglia — correttore non aggiornato")
        raise typer.Exit(0)

    logger.info(
        f"train: correttore salvato → {out_path} | "
        f"rmse_base={metrics['rmse_base']} rmse_model={metrics['rmse_model']} "
        f"improvement={metrics['improvement_pct']:.1f}% "
        f"n_train={int(metrics['n_train'])} n_test={int(metrics['n_test'])}"
    )

    # Per-hour RMSE sul test set
    test = dataset[dataset["split"] == "test"].copy()
    corrector = load_corrector(model_dir)
    if corrector is not None and not test.empty:
        X_test = test[FEATURES].copy()
        X_test["location_id"] = X_test["location_id"].astype("category")
        preds = corrector.predict(X_test)
        hourly = _per_hour_rmse(test, preds)
        for h, m in sorted(hourly.items()):
            logger.info(f"  h={h:02d}  base={m['rmse_base']:.3f}  model={m['rmse_model']:.3f}")


@app.command("eval")
def cmd_eval(
    db_path:    Path = DB_OPTION,
    config_dir: Path = CONFIG_DIR_OPTION,
    model_dir:  Path = _MODEL_DIR_OPTION,
) -> None:
    """Valuta il correttore attivo: RMSE per-ora corretto vs baseline sul test set."""
    corrector = load_corrector(model_dir)
    if corrector is None:
        logger.warning(f"eval: nessun correttore in {model_dir / CORRECTOR_FILENAME}")
        raise typer.Exit(0)

    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
        locations, _ = load_configs(config_dir)
        dataset = build_delta_dataset(db, locations)

    if dataset is None:
        logger.warning("eval: dati insufficienti per la valutazione")
        raise typer.Exit(0)

    test = dataset[dataset["split"] == "test"].copy()
    if test.empty:
        logger.warning("eval: nessuna riga nel test set")
        raise typer.Exit(0)

    X_test = test[FEATURES].copy()
    X_test["location_id"] = X_test["location_id"].astype("category")
    preds = corrector.predict(X_test)

    y = test["delta"].to_numpy()
    rmse_base  = float(np.sqrt(np.mean(y**2)))
    rmse_model = float(np.sqrt(np.mean((y - preds)**2)))
    improvement_pct = 100.0 * (rmse_base - rmse_model) / rmse_base if rmse_base > 0 else 0.0

    logger.info(
        f"eval: rmse_base={rmse_base:.4f} rmse_model={rmse_model:.4f} "
        f"improvement={improvement_pct:.1f}% n_test={len(test)}"
    )
    hourly = _per_hour_rmse(test, preds)
    for h, m in sorted(hourly.items()):
        logger.info(f"  h={h:02d}  base={m['rmse_base']:.3f}  model={m['rmse_model']:.3f}")


@app.command("status")
def cmd_status(
    db_path: Path = DB_OPTION,
) -> None:
    """Stato osservazioni realtime per location: giorni, slot/giorno, range date."""
    locations, _ = load_configs()
    with DuckDBClient(db_path=db_path, read_only=True) as db:
        for loc_id in sorted(locations):
            rows = db.execute("""
                SELECT
                    local_date,
                    COUNT(DISTINCT hour_slot) AS slots
                FROM (
                    SELECT
                        CAST(ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome' AS DATE) AS local_date,
                        HOUR(ts AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Rome')         AS hour_slot
                    FROM observations
                    WHERE granularity = 'realtime'
                      AND location_id = ?
                      AND temp_c IS NOT NULL
                ) h
                GROUP BY local_date
                HAVING slots >= 1
                ORDER BY local_date
            """, [loc_id]).fetchall()

            if not rows:
                logger.info(f"[{loc_id}] nessun dato realtime")
                continue

            n_days = len(rows)
            avg_slots = sum(r[1] for r in rows) / n_days
            first_date = str(rows[0][0])
            last_date  = str(rows[-1][0])
            logger.info(
                f"[{loc_id}] giorni={n_days} slot_avg={avg_slots:.1f} "
                f"da={first_date} a={last_date}"
            )


if __name__ == "__main__":
    app()
