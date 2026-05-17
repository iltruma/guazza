"""LightGBM quantile regression + CQR calibration per previsioni giornaliere.

Per ogni target (tmin_c, tmax_c, precip_mm):
  - 5 modelli LightGBM (alpha = 0.05, 0.10, 0.50, 0.90, 0.95)
  - CQR correction per 2 CI level (80% e 90%), stratificata per bucket lead time

Walk-forward CV con embargo 7 giorni (D-002).
CQR stratificato per bucket lead time (D-003).

Persistenza: pickle in MODEL_DIR (default: data/models/ locale,
/var/lib/guazza/models/ in produzione via env MODEL_DIR).
"""

from __future__ import annotations

import os
import pickle
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from loguru import logger

from guazza.storage import DuckDBClient

_DEFAULT_MODEL_DIR = Path(os.environ.get("MODEL_DIR", "data/models"))

QUANTILES: list[float] = [0.05, 0.10, 0.50, 0.90, 0.95]
TARGETS: list[str] = ["tmin_c", "tmax_c", "precip_mm"]

# Mapping target → colonna ensemble mean in features_daily
_TARGET_NWP_MEAN: dict[str, str] = {
    "tmin_c":    "nwp_tmin_mean",
    "tmax_c":    "nwp_tmax_mean",
    "precip_mm": "nwp_precip_mean",
}

# Lead time bucket per CQR stratification
LEAD_BUCKETS: dict[str, tuple[int, int]] = {
    "0-6h":   (0, 6),
    "6-12h":  (6, 12),
    "12-24h": (12, 24),
    "24-48h": (24, 48),
    "48-72h": (48, 72),
    "72h+":   (72, 9999),
}

FEATURE_COLS: list[str] = [
    # NWP per modello (5 modelli)
    "ecmwf_tmin_c",  "ecmwf_tmax_c",  "ecmwf_precip_mm",  "ecmwf_humidity_pct",  "ecmwf_wind_ms",
    "icon_tmin_c",   "icon_tmax_c",   "icon_precip_mm",   "icon_humidity_pct",   "icon_wind_ms",
    "icond2_tmin_c", "icond2_tmax_c", "icond2_precip_mm", "icond2_humidity_pct", "icond2_wind_ms",
    "gfs_tmin_c",    "gfs_tmax_c",    "gfs_precip_mm",    "gfs_humidity_pct",    "gfs_wind_ms",
    "arome_tmin_c",  "arome_tmax_c",  "arome_precip_mm",  "arome_humidity_pct",  "arome_wind_ms",
    "icon2i_tmin_c", "icon2i_tmax_c", "icon2i_precip_mm", "icon2i_humidity_pct", "icon2i_wind_ms",
    # Ensemble stats
    "nwp_tmin_mean", "nwp_tmin_spread",
    "nwp_tmax_mean", "nwp_tmax_spread",
    "nwp_precip_mean", "nwp_precip_spread",
    # Obs giorno precedente (lookahead-safe)
    "obs_tmin_c", "obs_tmax_c", "obs_precip_mm", "obs_humidity_pct",
    # Ring pluviometrici upstream (giorno precedente — lookahead-safe)
    "ring1_precip_d1_mean", "ring1_precip_d1_max",
    "ring2_precip_d1_mean", "ring2_precip_d1_max",
    "ring3_precip_d1_mean", "ring3_precip_d1_max",
    # Climatologia mensile
    "clim_tmin_mean", "clim_tmin_std",
    "clim_tmax_mean", "clim_tmax_std",
    "clim_precip_mean", "clim_precip_std",
    # Calendario
    "month", "day_of_year",
    # Lead time
    "lead_time_h",
    # Location (categorica)
    "location_id",
]

CATEGORICAL_COLS: list[str] = ["location_id"]


@dataclass
class CQRCorrection:
    """q_hat per CI level (80% e 90%). Corregge [q_lo - q_hat, q_hi + q_hat]."""
    ci80: float  # corregge [q10_pred, q90_pred]
    ci90: float  # corregge [q05_pred, q95_pred]
    n_cal: int   # dimensione calibration set usata


@dataclass
class ModelBundle:
    """Modelli quantile + CQR corrections per un singolo target."""
    models: dict[float, lgb.LGBMRegressor]
    cqr: dict[str, CQRCorrection]   # bucket_label → correction


@dataclass
class TrainingArtifacts:
    """Artefatti completi del training: modelli + calibrazione + metadati."""
    targets: dict[str, ModelBundle]  # target → bundle
    feature_cols: list[str]
    categorical_cols: list[str]
    trained_at: datetime
    n_train: int
    n_cal: int


def load_features(db: DuckDBClient) -> pd.DataFrame:
    """Carica features_daily da DuckDB in un DataFrame pandas.

    location_id viene castato a Categorical per LightGBM.
    target_date a datetime.date per ordinamento temporale.
    """
    df = db.execute("SELECT * FROM features_daily").df()
    df["location_id"] = df["location_id"].astype("category")
    df["target_date"] = pd.to_datetime(df["target_date"]).dt.date
    df = df.sort_values("target_date").reset_index(drop=True)
    logger.info(f"features_daily: {len(df)} righe, {df['target_date'].min()} → {df['target_date'].max()}")
    return df


def _lgbm_params(quantile: float) -> dict[str, Any]:
    return {
        "objective": "quantile",
        "alpha": quantile,
        "metric": "quantile",
        "n_estimators": 500,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_child_samples": 20,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "reg_alpha": 0.1,
        "reg_lambda": 0.1,
        "random_state": 42,
        "n_jobs": -1,
        "verbose": -1,
    }


def _train_lgbm(X: pd.DataFrame, y: pd.Series, quantile: float) -> lgb.LGBMRegressor:
    model = lgb.LGBMRegressor(**_lgbm_params(quantile))
    model.fit(X, y, categorical_feature=CATEGORICAL_COLS)
    return model


def _compute_cqr(
    models_q: dict[float, lgb.LGBMRegressor],
    X_cal: pd.DataFrame,
    y_cal: pd.Series,
    lead_h: pd.Series,
) -> dict[str, CQRCorrection]:
    """Computa CQR corrections stratificate per bucket lead time.

    Per ogni bucket: q_hat = quantile (1-alpha)(1+1/n) dei conformity scores.
    Se il bucket ha meno di 10 campioni, usa tutti i dati del cal set (fallback).
    """
    mask = y_cal.notna()
    X = X_cal.loc[mask]
    y = y_cal.loc[mask]
    lh = lead_h.loc[mask]

    # Predizioni sul cal set (tutti i quantili in un colpo)
    pred: dict[float, np.ndarray] = {
        q: m.predict(X) for q, m in models_q.items()
    }

    cqr: dict[str, CQRCorrection] = {}
    for label, (lo, hi) in LEAD_BUCKETS.items():
        bucket_idx = (lh >= lo) & (lh < hi)
        if bucket_idx.sum() < 10:
            # Fallback: calibra su tutti i dati disponibili
            bucket_idx = pd.Series(True, index=lh.index)
        n = int(bucket_idx.sum())

        y_b = y[bucket_idx].values

        q_hat_90 = _cqr_q_hat(
            pred[0.05][bucket_idx.values],
            pred[0.95][bucket_idx.values],
            y_b, alpha=0.10, n=n,
        )
        q_hat_80 = _cqr_q_hat(
            pred[0.10][bucket_idx.values],
            pred[0.90][bucket_idx.values],
            y_b, alpha=0.20, n=n,
        )
        cqr[label] = CQRCorrection(ci80=q_hat_80, ci90=q_hat_90, n_cal=n)

    return cqr


def _cqr_q_hat(q_lo: np.ndarray, q_hi: np.ndarray, y: np.ndarray, alpha: float, n: int) -> float:
    """q_hat per CQR: quantile (1-alpha)(1+1/n) dei conformity scores E_i = max(q_lo-y, y-q_hi)."""
    scores = np.maximum(q_lo - y, y - q_hi)
    level = min(1.0, (1 - alpha) * (1 + 1 / n))
    return float(np.quantile(scores, level))


def _pinball(y: np.ndarray, q: np.ndarray, alpha: float) -> float:
    err = y - q
    return float(np.mean(np.where(err >= 0, alpha * err, (alpha - 1) * err)))


def crps_from_quantiles(y: np.ndarray, preds: dict[float, np.ndarray]) -> float:
    """CRPS approssimato come media pesata delle pinball losses sui quantili.

    Approssimazione standard per set di quantili discreti (es. M4/M5 competition).
    CRPS ≈ 2 * mean_alpha(pinball(q_alpha, y, alpha))
    """
    return float(2 * np.mean([_pinball(y, q, alpha) for alpha, q in preds.items()]))


def _lead_time_bucket(lead_h: int) -> str:
    for label, (lo, hi) in LEAD_BUCKETS.items():
        if lo <= lead_h < hi:
            return label
    return "72h+"


def train_all(
    db: DuckDBClient,
    model_dir: Path | None = None,
    cal_days: int = 90,
) -> TrainingArtifacts:
    """Allena modelli su tutti i dati disponibili e calibra CQR.

    Split:
      train: target_date < max_date - cal_days
      cal:   target_date >= max_date - cal_days  (usato solo per CQR)

    Args:
        cal_days: giorni finali riservati al calibration set CQR.

    Returns:
        TrainingArtifacts salvati su disco.
    """
    if model_dir is None:
        model_dir = _DEFAULT_MODEL_DIR

    df = load_features(db)
    if df.empty:
        raise ValueError("features_daily è vuota. Esegui prima: features build")

    max_date = df["target_date"].max()
    cal_cutoff = pd.Timestamp(max_date) - pd.Timedelta(days=cal_days)
    cal_cutoff_date = cal_cutoff.date()

    df_train = df[df["target_date"] < cal_cutoff_date].copy()
    df_cal   = df[df["target_date"] >= cal_cutoff_date].copy()

    logger.info(
        f"Train: {len(df_train)} righe ({df_train['target_date'].min()} → {df_train['target_date'].max()})"
    )
    logger.info(
        f"Cal:   {len(df_cal)} righe ({df_cal['target_date'].min()} → {df_cal['target_date'].max()})"
    )

    bundles: dict[str, ModelBundle] = {}

    for target in TARGETS:
        col = f"target_{target}"
        mask_train = df_train[col].notna()
        X_tr = df_train.loc[mask_train, FEATURE_COLS]
        y_tr = df_train.loc[mask_train, col]

        logger.info(f"[{target}] training su {len(y_tr)} righe con {len(QUANTILES)} quantili")

        models_q: dict[float, lgb.LGBMRegressor] = {}
        for q in QUANTILES:
            models_q[q] = _train_lgbm(X_tr, y_tr, q)
            logger.debug(f"[{target}] q={q:.2f} addestrato")

        X_cal = df_cal[FEATURE_COLS]
        y_cal = df_cal[col]
        lead_h = df_cal["lead_time_h"]

        cqr = _compute_cqr(models_q, X_cal, y_cal, lead_h)
        logger.info(
            f"[{target}] CQR 0-6h → ci80={cqr['0-6h'].ci80:.3f} ci90={cqr['0-6h'].ci90:.3f}"
        )

        bundles[target] = ModelBundle(models=models_q, cqr=cqr)

    artifacts = TrainingArtifacts(
        targets=bundles,
        feature_cols=FEATURE_COLS,
        categorical_cols=CATEGORICAL_COLS,
        trained_at=datetime.now(tz=UTC),
        n_train=len(df_train),
        n_cal=len(df_cal),
    )

    _save_artifacts(artifacts, model_dir)
    return artifacts


def _save_artifacts(artifacts: TrainingArtifacts, model_dir: Path) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_dir / "artifacts.pkl"
    with open(path, "wb") as f:
        pickle.dump(artifacts, f)
    logger.info(f"Artefatti salvati: {path}")


def load_artifacts(model_dir: Path | None = None) -> TrainingArtifacts:
    """Carica artefatti dal disco."""
    if model_dir is None:
        model_dir = _DEFAULT_MODEL_DIR
    path = model_dir / "artifacts.pkl"
    if not path.exists():
        raise FileNotFoundError(f"Artefatti non trovati: {path}. Esegui prima: train run")
    with open(path, "rb") as f:
        return pickle.load(f)  # type: ignore[no-any-return]  # noqa: S301


def predict(
    artifacts: TrainingArtifacts,
    X: pd.DataFrame,
    lead_h: int = 0,
) -> dict[str, dict[str, float]]:
    """Genera predizioni con CI CQR-aggiustati per una singola riga.

    Returns:
        {target: {"p05": ..., "p10": ..., "p50": ..., "p90": ..., "p95": ...,
                  "ci80_lo": ..., "ci80_hi": ..., "ci90_lo": ..., "ci90_hi": ...}}
    """
    X = X.copy()
    for col in artifacts.categorical_cols:
        if col in X.columns:
            X[col] = X[col].astype("category")

    bucket = _lead_time_bucket(lead_h)
    out: dict[str, dict[str, float]] = {}

    for target, bundle in artifacts.targets.items():
        preds_q: dict[str, float] = {}
        for q, model in bundle.models.items():
            preds_q[f"p{int(q*100):02d}"] = float(model.predict(X)[0])

        corr = bundle.cqr.get(bucket, bundle.cqr["0-6h"])
        out[target] = {
            **preds_q,
            "ci80_lo": preds_q["p10"] - corr.ci80,
            "ci80_hi": preds_q["p90"] + corr.ci80,
            "ci90_lo": preds_q["p05"] - corr.ci90,
            "ci90_hi": preds_q["p95"] + corr.ci90,
        }

    return out


# ── Walk-forward CV ───────────────────────────────────────────────────────────

def walk_forward_cv(
    db: DuckDBClient,
    n_splits: int = 4,
    min_train_days: int = 365,
    embargo_days: int = 7,
    cal_fraction: float = 0.15,
) -> pd.DataFrame:
    """Walk-forward CV temporale con embargo 7 giorni.

    Ogni fold:
      train:  data dalla data più vecchia a test_start - embargo_days
      cal:    ultimi cal_fraction del train set (per CQR)
      test:   finestra di ~fold_size giorni

    Returns:
        DataFrame con colonne: split, target, mae, crps, coverage_80, coverage_90,
        skill_mae (vs nwp_mean), n_test.
    """
    df = load_features(db)
    if df.empty:
        raise ValueError("features_daily è vuota.")

    dates = sorted(df["target_date"].unique())
    n_dates = len(dates)

    available_start = min_train_days + embargo_days
    if n_dates <= available_start:
        raise ValueError(
            f"Dati insufficienti per CV: {n_dates} date, richieste >{available_start}"
        )

    fold_dates = dates[available_start:]
    fold_size = max(30, len(fold_dates) // n_splits)

    rows: list[dict[str, Any]] = []

    for i in range(n_splits):
        test_start = fold_dates[i * fold_size]
        end_idx = min((i + 1) * fold_size, len(fold_dates)) - 1
        test_end = fold_dates[end_idx]
        train_end_ts = pd.Timestamp(test_start) - pd.Timedelta(days=embargo_days)
        train_end = train_end_ts.date()

        df_train_full = df[df["target_date"] <= train_end]
        df_test       = df[(df["target_date"] >= test_start) & (df["target_date"] <= test_end)]

        if len(df_train_full) == 0 or len(df_test) == 0:
            logger.warning(f"Fold {i+1}: dati insufficienti, skip")
            continue

        # Calibration set: ultimi cal_fraction del train
        train_dates = sorted(df_train_full["target_date"].unique())
        n_cal_dates = max(30, int(len(train_dates) * cal_fraction))
        cal_start = train_dates[-n_cal_dates]
        df_cal   = df_train_full[df_train_full["target_date"] >= cal_start]
        df_train = df_train_full[df_train_full["target_date"] < cal_start]

        logger.info(
            f"Fold {i+1}/{n_splits}: train={len(df_train)} cal={len(df_cal)} "
            f"test={len(df_test)} ({test_start}→{test_end})"
        )

        for target in TARGETS:
            col = f"target_{target}"
            nwp_col = _TARGET_NWP_MEAN[target]

            mask_tr = df_train[col].notna()
            X_tr = df_train.loc[mask_tr, FEATURE_COLS]
            y_tr = df_train.loc[mask_tr, col]

            if len(y_tr) < 50:
                logger.warning(f"Fold {i+1} [{target}]: train troppo piccolo ({len(y_tr)}), skip")
                continue

            models_q: dict[float, lgb.LGBMRegressor] = {}
            for q in QUANTILES:
                models_q[q] = _train_lgbm(X_tr, y_tr, q)

            cqr = _compute_cqr(models_q, df_cal[FEATURE_COLS], df_cal[col], df_cal["lead_time_h"])

            # Valuta sul test set
            mask_te = df_test[col].notna()
            if mask_te.sum() == 0:
                continue

            X_te = df_test.loc[mask_te, FEATURE_COLS]
            y_te = df_test.loc[mask_te, col].values

            preds: dict[float, np.ndarray] = {q: m.predict(X_te) for q, m in models_q.items()}

            # MAE su mediana
            mae = float(np.mean(np.abs(y_te - preds[0.50])))

            # CRPS
            crps = crps_from_quantiles(y_te, preds)

            # Coverage empirica con CQR
            corr = cqr["0-6h"]  # tutti i test hanno lead_time_h=0
            ci80_lo = preds[0.10] - corr.ci80
            ci80_hi = preds[0.90] + corr.ci80
            ci90_lo = preds[0.05] - corr.ci90
            ci90_hi = preds[0.95] + corr.ci90
            cov80 = float(np.mean((y_te >= ci80_lo) & (y_te <= ci80_hi)))
            cov90 = float(np.mean((y_te >= ci90_lo) & (y_te <= ci90_hi)))

            # Skill score vs NWP mean (MAE-based)
            nwp_vals = df_test.loc[mask_te, nwp_col].values
            nwp_mask = ~np.isnan(nwp_vals)
            if nwp_mask.sum() > 0:
                mae_nwp = float(np.mean(np.abs(y_te[nwp_mask] - nwp_vals[nwp_mask])))
                mae_model = float(np.mean(np.abs(y_te[nwp_mask] - preds[0.50][nwp_mask])))
                skill = 1 - mae_model / mae_nwp if mae_nwp > 0 else float("nan")
            else:
                skill = float("nan")

            rows.append({
                "split": i + 1,
                "test_start": str(test_start),
                "test_end":   str(test_end),
                "target":     target,
                "mae":        round(mae, 3),
                "crps":       round(crps, 3),
                "coverage_80": round(cov80, 3),
                "coverage_90": round(cov90, 3),
                "skill_mae":  round(skill, 3) if not np.isnan(skill) else None,
                "n_test":     int(mask_te.sum()),
            })

    return pd.DataFrame(rows)
