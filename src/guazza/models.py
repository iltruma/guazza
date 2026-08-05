"""LightGBM quantile regression + CQR calibration per previsioni giornaliere.

Per ogni target (tmin_c, tmax_c, precip_mm):
  - 5 modelli LightGBM (alpha = 0.05, 0.10, 0.50, 0.90, 0.95)
  - CQR correction per 2 CI level (80% e 90%), stratificata per bucket lead time

Walk-forward CV con embargo 7 giorni (D-002).
CQR stratificato per bucket lead time (D-003).

Persistenza in MODEL_DIR (default: data/models/ locale, /var/lib/guazza/models/
in produzione via env MODEL_DIR): manifest artifacts.json + un file testo
LightGBM per (target, quantile). Niente pickle: un artefatto manomesso non può
eseguire codice al load e i file sono ispezionabili.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from loguru import logger

from guazza.features import NWP_FEATURE_COLS
from guazza.storage import DuckDBClient

_DEFAULT_MODEL_DIR = Path(os.environ.get("MODEL_DIR", "data/models"))

QUANTILES: list[float] = [0.05, 0.10, 0.50, 0.90, 0.95]
TARGETS: list[str] = ["tmin_c", "tmax_c", "precip_mm"]

# Soglia "wet day" per il classificatore binario pioggia/no.
# Allineata alla soglia del DLE (P(precip > 0.2mm) in indicators.yaml / build_signals).
# Corrisponde alla risoluzione minima del pluviometro SIR.
RAIN_THRESHOLD_MM: float = 0.2

# Ordine fisso dei quantili per il sort anti-crossing in predict / predict_frame.
# Le chiavi sono già in ordine (p05 < p10 < p50 < p90 < p95), ma i valori
# predetti dai 5 modelli indipendenti possono incrociarsi.
_Q_ORDER: list[str] = ["p05", "p10", "p50", "p90", "p95"]

# Mapping target → colonna ensemble mean in features_daily
_TARGET_NWP_MEAN: dict[str, str] = {
    "tmin_c":    "nwp_tmin_mean",
    "tmax_c":    "nwp_tmax_mean",
    "precip_mm": "nwp_precip_mean",
}

# Mapping target → colonna climatologia usata per inversione a predict time
_TARGET_CLIM_COL: dict[str, str] = {
    "tmin_c":    "clim_tmin_mean",
    "tmax_c":    "clim_tmax_mean",
    "precip_mm": "clim_precip_mean",
}

# Colonne target in features_daily: valore assoluto.
# Naming: per un target "tmin_c", absolute = "target_tmin_c".
_TARGET_COL_ABS: dict[str, str] = {
    "tmin_c":    "target_tmin_c",
    "tmax_c":    "target_tmax_c",
    "precip_mm": "target_precip_mm",
}


def _target_col(target: str) -> str:
    """Colonna target in features_daily per `target` (es. 'tmin_c')."""
    return _TARGET_COL_ABS[target]

# Lead time bucket per CQR stratification.
# features_daily ha lead_time_h in multipli di 24h (0, 24, 48, ..., 168):
# i bucket sono quindi giornalieri, non orari. Bucket orari produrrebbero
# strati sempre vuoti con fallback sistematico al bucket adiacente.
LEAD_BUCKETS: dict[str, tuple[int, int]] = {
    "D+0":  (0,   1),      # lead_time_h == 0 (nowcast)
    "D+1":  (1,  36),      # 24h
    "D+2":  (36, 60),      # 48h
    "D+3":  (60, 84),      # 72h
    "D+4":  (84, 108),     # 96h
    "D+5+": (108, 9999),   # 120h, 144h, 168h
}

FEATURE_COLS: list[str] = [
    # NWP per modello (4 modelli) — derivate da NWP_MODEL_PREFIXES, stessa fonte del
    # pivot wide in features.py: le due liste non possono divergere.
    *NWP_FEATURE_COLS,
    # Ensemble stats
    "nwp_tmin_mean", "nwp_tmin_spread",
    "nwp_tmax_mean", "nwp_tmax_spread",
    "nwp_precip_mean", "nwp_precip_spread",
    "nwp_pressure_mean", "nwp_pressure_spread",
    "nwp_cape_mean", "nwp_cape_spread",
    # Obs giorno precedente (lookahead-safe)
    "obs_tmin_c", "obs_tmax_c", "obs_precip_mm", "obs_humidity_pct",
    # Obs lag-2 e gradient termico
    "obs_tmin_d2", "obs_tmax_d2",
    "obs_tmin_gradient", "obs_tmax_gradient",
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
    "doy_sin", "doy_cos",
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
class IsotonicCalibration:
    """Calibrazione isotonica post-hoc per le probabilità del classificatore binario.

    Fit su (prob_raw, y_binary) del cal set. A predict time:
      prob_calibrated = np.interp(prob_raw, x_thresholds, y_calibrated)
    """
    x_thresholds: list[float]   # prob grezze (punti di breakpoint isotonica)
    y_calibrated: list[float]   # prob calibrate corrispondenti


@dataclass
class ModelBundle:
    """Modelli quantile + CQR corrections per un singolo target.

    In training i modelli sono LGBMRegressor; al load da disco sono Booster
    (ricostruiti da model string). L'API condivisa è .predict().
    """
    models: dict[float, lgb.LGBMRegressor | lgb.Booster]
    cqr: dict[str, CQRCorrection]   # bucket_label → correction


@dataclass
class ClassifierBundle:
    """Classificatore binario P(precip > RAIN_THRESHOLD_MM).

    Un solo LGBMClassifier (objective binary), calibrato isotonicamente
    sul cal set. Il bundle è separato da ModelBundle perché non ha
    quantili, CQR né init_score.
    """
    model: lgb.LGBMClassifier | lgb.Booster
    threshold_mm: float                        # 0.2 — persiste per riproducibilità
    calibration: IsotonicCalibration | None    # None = prob grezze (non ancora calibrato)


@dataclass
class TrainingArtifacts:
    """Artefatti completi del training: modelli + calibrazione + metadati."""
    targets: dict[str, ModelBundle]  # target → bundle
    feature_cols: list[str]
    categorical_cols: list[str]
    trained_at: datetime
    n_train: int
    n_cal: int
    # Target allenati con init_score=nwp_mean (il booster predice il residuo).
    # Persistito nel manifest per sapere a predict time quali target vanno sommati.
    init_score_targets: list[str] = field(default_factory=list)
    # Classificatore binario pioggia/no (hurdle model stadio 1). None = non addestrato.
    rain_classifier: ClassifierBundle | None = field(default=None)


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
        "n_estimators": 2000,
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


def train_lgbm(
    X: pd.DataFrame,
    y: pd.Series,
    quantile: float,
    X_val: pd.DataFrame | None = None,
    y_val: pd.Series | None = None,
    init_score: np.ndarray | None = None,
    init_score_val: np.ndarray | None = None,
) -> lgb.LGBMRegressor:
    model = lgb.LGBMRegressor(**_lgbm_params(quantile))
    if X_val is not None and y_val is not None and len(y_val) > 0:
        model.fit(
            X, y,
            categorical_feature=CATEGORICAL_COLS,
            init_score=init_score,
            eval_X=X_val,
            eval_y=y_val,
            eval_init_score=[init_score_val] if init_score_val is not None else None,
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
        )
    else:
        model.fit(X, y, categorical_feature=CATEGORICAL_COLS, init_score=init_score)
    return model


def _es_val_split(
    df: pd.DataFrame,
    end_date: pd.Timestamp,
    es_val_days: int = 30,
    embargo_days: int = 7,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Ritaglia df_fit e df_es_val con embargo dal confine end_date.

    Layout temporale:
      [--- df_fit ---][gap embargo][--- df_es_val es_val_days ---][gap embargo][end_date ...]

    Args:
        df: DataFrame con colonna target_date (date).
        end_date: inizio del set successivo (cal set o test set).
        es_val_days: ampiezza della finestra early-stop validation.
        embargo_days: gap prima e dopo df_es_val.

    Returns:
        (df_fit, df_es_val). Se i dati sono insufficienti per entrambi i set
        (df_fit < 30 righe o df_es_val vuoto), ritorna (df, empty) — il chiamante
        deve gestire il fallback a training senza early stopping.
    """
    es_end   = end_date - pd.Timedelta(days=embargo_days)
    es_start = es_end   - pd.Timedelta(days=es_val_days)
    fit_end  = es_start - pd.Timedelta(days=embargo_days)

    df_es_val = df[
        (df["target_date"] >= es_start.date()) & (df["target_date"] < es_end.date())
    ].copy()
    df_fit = df[df["target_date"] < fit_end.date()].copy()

    if len(df_fit) < 30 or len(df_es_val) == 0:
        return df, pd.DataFrame()

    return df_fit, df_es_val


def _train_rain_classifier(
    X_fit: pd.DataFrame,
    y_precip_fit: pd.Series,
    X_cal: pd.DataFrame,
    y_precip_cal: pd.Series,
    X_val: pd.DataFrame | None = None,
    y_precip_val: pd.Series | None = None,
    threshold_mm: float = RAIN_THRESHOLD_MM,
) -> ClassifierBundle:
    """Allena il classificatore binario pioggia/no e calibra isotonicamente.

    Args:
        X_fit: feature del fit set (stesso FEATURE_COLS dei regressori).
        y_precip_fit: target_precip_mm del fit set (valore grezzo, non binarizzato).
        X_cal: feature del cal set (usato per la calibrazione isotonica).
        y_precip_cal: target_precip_mm del cal set.
        X_val: feature del validation set per early stopping (opzionale).
        y_precip_val: target_precip_mm del validation set (opzionale).
        threshold_mm: soglia wet day (default RAIN_THRESHOLD_MM).

    Returns:
        ClassifierBundle con modello addestrato e calibrazione isotonica.
    """
    y_fit = (y_precip_fit > threshold_mm).astype(int)
    y_cal_bin = (y_precip_cal > threshold_mm).astype(int)

    n_dry = int((y_fit == 0).sum())
    n_wet = int((y_fit == 1).sum())
    spw = n_dry / n_wet if n_wet > 0 else 1.0

    params: dict[str, Any] = {
        "objective": "binary",
        "metric": ["binary_logloss", "auc"],
        "n_estimators": 2000,
        "learning_rate": 0.05,
        "num_leaves": 31,
        "min_child_samples": 20,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "reg_alpha": 0.1,
        "reg_lambda": 0.1,
        "scale_pos_weight": spw,
        "random_state": 42,
        "n_jobs": -1,
        "verbose": -1,
    }

    model = lgb.LGBMClassifier(**params)

    if X_val is not None and y_precip_val is not None and len(y_precip_val) > 0:
        y_val_bin = (y_precip_val > threshold_mm).astype(int)
        model.fit(
            X_fit, y_fit,
            categorical_feature=CATEGORICAL_COLS,
            eval_X=X_val,
            eval_y=y_val_bin,
            callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
        )
    else:
        model.fit(X_fit, y_fit, categorical_feature=CATEGORICAL_COLS)

    logger.info(
        f"[rain_clf] addestrato: {n_wet}/{n_wet+n_dry} wet ({100*n_wet/(n_wet+n_dry):.1f}%), "
        f"scale_pos_weight={spw:.2f}"
    )

    # Calibrazione isotonica sul cal set
    calibration: IsotonicCalibration | None = None
    mask_cal = y_precip_cal.notna()
    if mask_cal.sum() >= 20:
        X_cal_clean = X_cal.loc[mask_cal].copy()
        for col in CATEGORICAL_COLS:
            if col in X_cal_clean.columns:
                X_cal_clean[col] = X_cal_clean[col].astype("category")
        prob_raw = np.asarray(model.predict_proba(X_cal_clean))[:, 1]
        y_cal_clean = y_cal_bin.loc[mask_cal].values

        from sklearn.isotonic import IsotonicRegression  # noqa: PLC0415
        iso = IsotonicRegression(out_of_bounds="clip")
        iso.fit(prob_raw, y_cal_clean)
        # Salva solo i breakpoint (punti unici, non tutto il vettore fitted)
        x_thresh = iso.X_thresholds_.tolist()
        y_calib  = iso.y_thresholds_.tolist()
        calibration = IsotonicCalibration(x_thresholds=x_thresh, y_calibrated=y_calib)
        logger.info(f"[rain_clf] calibrazione isotonica: {len(x_thresh)} breakpoint")
    else:
        logger.warning(f"[rain_clf] cal set troppo piccolo ({mask_cal.sum()} righe), salto calibrazione isotonica")

    return ClassifierBundle(
        model=model,
        threshold_mm=threshold_mm,
        calibration=calibration,
    )


def _apply_rain_calibration(prob_raw: np.ndarray, calibration: IsotonicCalibration | None) -> np.ndarray:
    """Applica la calibrazione isotonica alle probabilità grezze.

    Se calibration è None, restituisce prob_raw invariato.
    """
    if calibration is None:
        return prob_raw
    return np.asarray(np.interp(
        prob_raw,
        calibration.x_thresholds,
        calibration.y_calibrated,
    ))


def _make_init_score(df: pd.DataFrame, target: str, mask: pd.Series) -> np.ndarray:
    """Calcola il vettore init_score per il training: nwp_mean con fallback a clim, poi 0."""
    nwp_col  = _TARGET_NWP_MEAN[target]
    clim_col = _TARGET_CLIM_COL[target]
    return np.asarray(
        df.loc[mask, nwp_col]
        .fillna(df.loc[mask, clim_col])
        .fillna(0.0)
    )


def _predict_level(
    model: lgb.LGBMRegressor | lgb.Booster,
    X: pd.DataFrame,
    target: str,
    use_init_score: bool,
) -> np.ndarray:
    """Predizione LightGBM riportata a livello assoluto.

    Se use_init_score=True il booster ha imparato il residuo rispetto a
    nwp_mean; somma nwp_mean (con fallback a clim, poi 0) per tornare al
    livello assoluto. Se False, restituisce il raw predict invariato.
    """
    raw = np.asarray(model.predict(X))
    if not use_init_score:
        return raw
    nwp_col  = _TARGET_NWP_MEAN[target]
    clim_col = _TARGET_CLIM_COL[target]
    base = np.asarray(
        X[nwp_col]
        .fillna(X[clim_col])
        .fillna(0.0)
    )
    return raw + base


def _compute_cqr(
    models_q: dict[float, lgb.LGBMRegressor | lgb.Booster],
    X_cal: pd.DataFrame,
    y_cal: pd.Series,
    lead_h: pd.Series,
    target: str = "",
    use_init_score: bool = False,
) -> dict[str, CQRCorrection]:
    """Computa CQR corrections stratificate per bucket lead time.

    Per ogni bucket: q_hat = quantile (1-alpha)(1+1/n) dei conformity scores.
    Se il bucket ha meno di 10 campioni, usa il bucket adiacente più vicino
    con dati sufficienti. Solo come ultima risorsa (nessun bucket adiacente
    disponibile) usa tutti i dati del cal set.
    """
    mask = y_cal.notna()
    X = X_cal.loc[mask]
    y = y_cal.loc[mask]
    lh = lead_h.loc[mask]

    # Predizioni sul cal set a livello assoluto (se use_init_score, somma nwp_mean)
    pred: dict[float, np.ndarray] = {
        q: _predict_level(m, X, target, use_init_score) for q, m in models_q.items()
    }

    # Pre-calcola quanti campioni ha ogni bucket
    bucket_labels = list(LEAD_BUCKETS.keys())
    bucket_counts = {
        label: int(((lh >= lo) & (lh < hi)).sum())
        for label, (lo, hi) in LEAD_BUCKETS.items()
    }

    def _bucket_idx_for(target_label: str) -> pd.Series:
        """Ritorna la maschera per il bucket target o il più vicino con >=10 campioni."""
        lo_t, hi_t = LEAD_BUCKETS[target_label]
        idx_t = (lh >= lo_t) & (lh < hi_t)
        if idx_t.sum() >= 10:
            return idx_t
        # Cerca il bucket adiacente con dati sufficienti, partendo dal più vicino
        target_pos = bucket_labels.index(target_label)
        for distance in range(1, len(bucket_labels)):
            for direction in [-1, 1]:
                neighbor_pos = target_pos + direction * distance
                if 0 <= neighbor_pos < len(bucket_labels):
                    neighbor = bucket_labels[neighbor_pos]
                    lo_n, hi_n = LEAD_BUCKETS[neighbor]
                    idx_n = (lh >= lo_n) & (lh < hi_n)
                    if idx_n.sum() >= 10:
                        logger.debug(
                            f"CQR bucket '{target_label}' ({bucket_counts[target_label]} campioni) "
                            f"→ fallback su '{neighbor}' ({bucket_counts[neighbor]} campioni)"
                        )
                        return idx_n
        # Ultima risorsa: tutti i dati disponibili
        logger.warning(
            f"CQR bucket '{target_label}': nessun bucket adiacente con >=10 campioni, "
            f"uso tutti i dati ({mask.sum()} campioni)"
        )
        return pd.Series(True, index=lh.index)

    cqr: dict[str, CQRCorrection] = {}
    for label in bucket_labels:
        bucket_idx = _bucket_idx_for(label)
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
    raise ValueError(f"lead_h={lead_h} fuori da tutti i LEAD_BUCKETS")


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
        raise ValueError("features_daily è vuota. Popolala con ingest + train run")

    max_date = df["target_date"].max()
    cal_cutoff = pd.Timestamp(max_date) - pd.Timedelta(days=cal_days)
    cal_cutoff_date = cal_cutoff.date()

    df_fit, df_es_val = _es_val_split(df, end_date=pd.Timestamp(cal_cutoff_date))
    has_es_val = len(df_es_val) > 0
    df_cal = df[df["target_date"] >= cal_cutoff_date].copy()

    logger.info(
        f"Fit:   {len(df_fit)} righe ({df_fit['target_date'].min()} → {df_fit['target_date'].max()})"
    )
    if has_es_val:
        logger.info(
            f"ES-val: {len(df_es_val)} righe ({df_es_val['target_date'].min()} → {df_es_val['target_date'].max()})"
        )
    logger.info(
        f"Cal:   {len(df_cal)} righe ({df_cal['target_date'].min()} → {df_cal['target_date'].max()})"
    )

    bundles: dict[str, ModelBundle] = {}

    for target in TARGETS:
        col = _target_col(target)
        mask_train = df_fit[col].notna()
        X_tr = df_fit.loc[mask_train, FEATURE_COLS]
        y_tr = df_fit.loc[mask_train, col]

        logger.info(f"[{target}] training su {len(y_tr)} righe con {len(QUANTILES)} quantili")

        models_q: dict[float, lgb.LGBMRegressor | lgb.Booster] = {}
        for q in QUANTILES:
            init_tr  = _make_init_score(df_fit, target, mask_train)
            init_val = _make_init_score(df_es_val, target, df_es_val[col].notna()) if has_es_val else None
            if has_es_val:
                mask_val = df_es_val[col].notna()
                X_val_es = df_es_val.loc[mask_val, FEATURE_COLS]
                y_val_es = df_es_val.loc[mask_val, col]
                models_q[q] = train_lgbm(
                    X_tr, y_tr, q,
                    X_val=X_val_es, y_val=y_val_es,
                    init_score=init_tr, init_score_val=init_val,
                )
            else:
                models_q[q] = train_lgbm(X_tr, y_tr, q, init_score=init_tr)
            logger.debug(f"[{target}] q={q:.2f} addestrato")

        X_cal = df_cal[FEATURE_COLS]
        y_cal = df_cal[col]
        lead_h = df_cal["lead_time_h"]

        cqr = _compute_cqr(models_q, X_cal, y_cal, lead_h, target=target, use_init_score=True)
        logger.info(
            f"[{target}] CQR D+0 → ci80={cqr['D+0'].ci80:.3f} ci90={cqr['D+0'].ci90:.3f}"
        )

        bundles[target] = ModelBundle(models=models_q, cqr=cqr)

    # Classificatore binario pioggia/no (hurdle model stadio 1)
    mask_precip_fit = df_fit["target_precip_mm"].notna()
    mask_precip_cal = df_cal["target_precip_mm"].notna()
    if mask_precip_fit.sum() >= 50:
        rain_clf: ClassifierBundle | None = _train_rain_classifier(
            X_fit=df_fit.loc[mask_precip_fit, FEATURE_COLS],
            y_precip_fit=df_fit.loc[mask_precip_fit, "target_precip_mm"],
            X_cal=df_cal.loc[mask_precip_cal, FEATURE_COLS],
            y_precip_cal=df_cal.loc[mask_precip_cal, "target_precip_mm"],
            X_val=df_es_val.loc[df_es_val["target_precip_mm"].notna(), FEATURE_COLS] if has_es_val else None,
            y_precip_val=df_es_val.loc[df_es_val["target_precip_mm"].notna(), "target_precip_mm"] if has_es_val else None,
        )
    else:
        rain_clf = None
        logger.warning("[rain_clf] dati insufficienti per il classificatore, saltato")

    artifacts = TrainingArtifacts(
        targets=bundles,
        rain_classifier=rain_clf,
        feature_cols=FEATURE_COLS,
        categorical_cols=CATEGORICAL_COLS,
        trained_at=datetime.now(tz=UTC),
        n_train=len(df_fit),
        n_cal=len(df_cal),
        init_score_targets=list(TARGETS),
    )

    _save_artifacts(artifacts, model_dir)
    return artifacts


def _save_artifacts(artifacts: TrainingArtifacts, model_dir: Path) -> None:
    """Persiste manifest JSON + un file model-string LightGBM per (target, quantile)."""
    model_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "format_version": 1,
        "trained_at": artifacts.trained_at.isoformat(),
        "n_train": artifacts.n_train,
        "n_cal": artifacts.n_cal,
        "feature_cols": artifacts.feature_cols,
        "categorical_cols": artifacts.categorical_cols,
        "init_score_targets": artifacts.init_score_targets,
        "targets": {},
    }
    n_files = 0
    for target, bundle in artifacts.targets.items():
        model_files: dict[str, str] = {}
        for q, model in bundle.models.items():
            booster = model.booster_ if isinstance(model, lgb.LGBMRegressor) else model
            filename = f"{target}_q{int(q * 100):02d}.txt"
            (model_dir / filename).write_text(booster.model_to_string())
            model_files[str(q)] = filename
            n_files += 1
        manifest["targets"][target] = {
            "models": model_files,
            "cqr": {label: asdict(corr) for label, corr in bundle.cqr.items()},
        }
    if artifacts.rain_classifier is not None:
        clf = artifacts.rain_classifier
        booster = clf.model.booster_ if isinstance(clf.model, lgb.LGBMClassifier) else clf.model
        clf_filename = "rain_clf.txt"
        (model_dir / clf_filename).write_text(booster.model_to_string())
        n_files += 1
        manifest["rain_classifier"] = {
            "model_file": clf_filename,
            "threshold_mm": clf.threshold_mm,
            "calibration": {
                "x_thresholds": clf.calibration.x_thresholds,
                "y_calibrated": clf.calibration.y_calibrated,
            } if clf.calibration is not None else None,
        }
    else:
        manifest["rain_classifier"] = None
    manifest_path = model_dir / "artifacts.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    logger.info(f"Artefatti salvati: {manifest_path} (+{n_files} modelli .txt)")


def load_artifacts(model_dir: Path | None = None) -> TrainingArtifacts:
    """Carica artefatti dal disco (manifest JSON + model string LightGBM)."""
    if model_dir is None:
        model_dir = _DEFAULT_MODEL_DIR
    manifest_path = model_dir / "artifacts.json"
    if not manifest_path.exists():
        if (model_dir / "artifacts.pkl").exists():
            raise RuntimeError(
                f"Trovato artifacts.pkl obsoleto in {model_dir}: il formato pickle "
                "è stato sostituito da artifacts.json + modelli .txt. "
                "Riesegui: train run"
            )
        # Suggerimenti per l'utente quando gli artefatti mancano al path di default.
        # In produzione k8s il path è /var/lib/guazza/models; in locale è data/models.
        hint = ""
        if model_dir == _DEFAULT_MODEL_DIR:
            alt = Path("data/models") / "artifacts.json"
            if alt.exists():
                hint = (
                    f" Trovati in {alt}: passa --model-dir data/models "
                    f"o esporta MODEL_DIR=data/models."
                )
        raise FileNotFoundError(
            f"Artefatti non trovati: {manifest_path}.{hint} Esegui prima: train run"
        )

    manifest = json.loads(manifest_path.read_text())
    targets: dict[str, ModelBundle] = {}
    for target, entry in manifest["targets"].items():
        models: dict[float, lgb.LGBMRegressor | lgb.Booster] = {
            float(q): lgb.Booster(model_str=(model_dir / filename).read_text())
            for q, filename in entry["models"].items()
        }
        cqr = {label: CQRCorrection(**corr) for label, corr in entry["cqr"].items()}
        targets[target] = ModelBundle(models=models, cqr=cqr)

    rain_classifier: ClassifierBundle | None = None
    clf_entry = manifest.get("rain_classifier")
    if clf_entry is not None:
        clf_model = lgb.Booster(model_str=(model_dir / clf_entry["model_file"]).read_text())
        calib_data = clf_entry.get("calibration")
        calibration = (
            IsotonicCalibration(
                x_thresholds=calib_data["x_thresholds"],
                y_calibrated=calib_data["y_calibrated"],
            )
            if calib_data is not None
            else None
        )
        rain_classifier = ClassifierBundle(
            model=clf_model,
            threshold_mm=clf_entry["threshold_mm"],
            calibration=calibration,
        )

    return TrainingArtifacts(
        targets=targets,
        rain_classifier=rain_classifier,
        feature_cols=manifest["feature_cols"],
        categorical_cols=manifest["categorical_cols"],
        trained_at=datetime.fromisoformat(manifest["trained_at"]),
        n_train=manifest["n_train"],
        n_cal=manifest["n_cal"],
        init_score_targets=manifest.get("init_score_targets", []),
    )


def _apply_cqr(
    preds_q: dict[str, float], bundle: ModelBundle, bucket: str
) -> dict[str, float]:
    """Aggiunge i bound CI CQR-aggiustati ai 5 quantili predetti per un target.

    Nested CI guarantee: il bound al 90% contiene sempre quello all'80%.
    CQR non garantisce naturalmente questa proprietà se `q_hat_90 < q_hat_80`
    (succede per `precip_mm` con cal set molto zero-inflated: il quantile
    90% dei conformity scores su q05-q95 risulta < quantile 80% dei
    conformity scores su q10-q90 perché q95-q05 è stretto e centrato).
    Forziamo `ci90_lo = min(ci90_lo, ci80_lo)` e `ci90_hi = max(...)` per
    rispettare la proprietà teorica del CQR nested.
    """
    corr = bundle.cqr.get(bucket, bundle.cqr["D+0"])
    ci80_lo = preds_q["p10"] - corr.ci80
    ci80_hi = preds_q["p90"] + corr.ci80
    ci90_lo = preds_q["p05"] - corr.ci90
    ci90_hi = preds_q["p95"] + corr.ci90
    # Nested CI: la CI al 90% contiene sempre la CI all'80%.
    ci90_lo = min(ci90_lo, ci80_lo)
    ci90_hi = max(ci90_hi, ci80_hi)
    return {
        **preds_q,
        "ci80_lo": ci80_lo,
        "ci80_hi": ci80_hi,
        "ci90_lo": ci90_lo,
        "ci90_hi": ci90_hi,
    }


# Learning rate ACI (γ): D-019 specifica 0.005. Il drift da correggere è
# stagionale (scala mesi), non giornaliero. γ=0.02 sarebbe 4× troppo aggressivo
# — inseguirebbe il rumore meteo invece del trend di calibrazione.
# Punto unico di verità: importato da pipeline.py per evitare drift doc↔codice.
ACI_LEARNING_RATE: float = 0.005

# Fattore di correzione ACI clampato a [MIN, MAX] per evitare bande patologiche
# quando alpha_t si avvicina al clamp eps=0.01 (drift prolungato). Senza clamp,
# f = alpha_target/alpha_t può arrivare a 10+ → banda ±30°C inutili.
# 0.5..2.0 = la correzione può stringere al massimo del 50% o allargare al massimo
# del 100% rispetto al CQR baseline. Oltre è rumore.
ACI_CORRECTION_FACTOR_MIN: float = 0.5
ACI_CORRECTION_FACTOR_MAX: float = 2.0

# Cold start: prime N osservazioni prima che ACI sia affidabile. CQR statico
# (calcolato da train_all) è più conservativo e ci protegge. Dopo N obs,
# l'alpha_t corrente è già stabile.
ACI_COLD_START_N: int = 30


class AdaptiveConformalizer:
    """ACI (Gibbs & Candès 2021) per singolo livello α.

    Aggiusta `alpha_t` online ad ogni feedback di copertura, mantenendo la
    garanzia long-run marginal di copertura = 1-α anche sotto distribution
    shift (drift climatico, cambio modello). Il CQR statico fallisce in questi
    casi (vedi KI-023 — drift già in atto sui fold recenti).

    Algoritmo: alpha_{t+1} = clip(alpha_t + γ * (α_target − err_t), ε, 1−ε)
    dove err_t = 1 se miscoverage al tempo t, 0 altrimenti.

    Args:
        alpha_target: livello target (es. 0.10 per CI 90%, 0.20 per CI 80%).
        learning_rate: γ. Default ACI_LEARNING_RATE=0.005 (D-019). Il drift da
            correggere è stagionale (mesi); γ più alto inseguirebbe rumore giornaliero.
        eps: clamping per evitare alpha degeneri (0 o 1).
    """

    def __init__(
        self,
        alpha_target: float,
        learning_rate: float = ACI_LEARNING_RATE,
        eps: float = 0.01,
    ) -> None:
        self.alpha_target = alpha_target
        self.gamma = learning_rate
        self.eps = eps
        self.alpha_t = alpha_target
        self.n_updates = 0
        self._err_sum = 0  # somma err_t per diagnostics

    def update(self, covered: bool) -> float:
        """Registra feedback di copertura al tempo t e aggiorna alpha_t.

        Returns:
            Nuovo alpha_t (per ispezione / logging).
        """
        err = 0 if covered else 1
        self._err_sum += err
        self.alpha_t = max(
            self.eps,
            min(1.0 - self.eps, self.alpha_t + self.gamma * (self.alpha_target - err)),
        )
        self.n_updates += 1
        return self.alpha_t

    def correct(self, offset: float) -> float:
        """Restituisce l'offset CQR-equivalente per il livello alpha_t corrente.

        `offset` è l'offset CQR calcolato al baseline (alpha_target). ACI lo
        aggiusta: alpha_t più alto → CI più stretto (offset più piccolo),
        alpha_t più basso → CI più largo. Approssimazione lineare attorno
        al baseline.

        ponytail: correct() è usato solo nei test sintetici (test_aci_*.py).
        In produzione il mapping alpha_t→CI passa da apply_aci_correction
        (scaling moltiplicativo del half-width CQR, più robusto).
        """
        delta_alpha = self.alpha_target - self.alpha_t
        return offset * (1.0 + 5.0 * delta_alpha)

    def to_dict(self) -> dict[str, float | int]:
        return {
            "alpha_target": self.alpha_target,
            "alpha_t": self.alpha_t,
            "n_updates": self.n_updates,
            "err_rate": self._err_sum / self.n_updates if self.n_updates else 0.0,
        }

    @classmethod
    def from_state(
        cls,
        alpha_target: float,
        alpha_t: float,
        n_updates: int,
        err_sum: int,
        learning_rate: float = ACI_LEARNING_RATE,
        eps: float = 0.01,
    ) -> AdaptiveConformalizer:
        """Ricostruisce ACI da state persistito (DuckDB o dizionario).

        Usato dopo `db.get_aci_state()` per ricaricare lo state al startup
        del job predict. Se n_updates == 0, alpha_t == alpha_target (cold start).
        """
        aci = cls(alpha_target=alpha_target, learning_rate=learning_rate, eps=eps)
        aci.alpha_t = max(eps, min(1.0 - eps, alpha_t))
        aci.n_updates = n_updates
        aci._err_sum = int(err_sum)
        return aci

    @property
    def err_rate(self) -> float:
        return self._err_sum / self.n_updates if self.n_updates else 0.0

    @property
    def err_sum(self) -> int:
        return self._err_sum


def get_aci_pair(
    db: DuckDBClient,
    target: str,
    lead_bucket: str,
    learning_rate: float = ACI_LEARNING_RATE,
) -> tuple[AdaptiveConformalizer, AdaptiveConformalizer]:
    """Carica (o crea) la coppia ACI per (target, lead_bucket) ai livelli 80%/90%.

    Returns:
        (aci_80, aci_90): istanze pronte per update/correct. Se assenti in DB,
        hanno alpha_t == alpha_target (cold start, CQR statico farà da fallback
        pratico finché n_updates < ACI_COLD_START_N).
    """
    state = db.get_aci_state(target, lead_bucket)
    if state is None:
        return (
            AdaptiveConformalizer(alpha_target=0.20, learning_rate=learning_rate),
            AdaptiveConformalizer(alpha_target=0.10, learning_rate=learning_rate),
        )
    return (
        AdaptiveConformalizer.from_state(
            alpha_target=0.20,
            alpha_t=state["alpha_t_80"],
            n_updates=state["n_updates"],
            err_sum=state["err_sum_80"],
            learning_rate=learning_rate,
        ),
        AdaptiveConformalizer.from_state(
            alpha_target=0.10,
            alpha_t=state["alpha_t_90"],
            n_updates=state["n_updates"],
            err_sum=state["err_sum_90"],
            learning_rate=learning_rate,
        ),
    )


def apply_aci_correction(
    ci80_lo: float,
    ci80_hi: float,
    ci90_lo: float,
    ci90_hi: float,
    aci_80: AdaptiveConformalizer,
    aci_90: AdaptiveConformalizer,
) -> tuple[float, float, float, float, str]:
    """Applica la correzione ACI ai bound CI. Restituisce (lo80, hi80, lo90, hi90, source).

    source ∈ {"aci", "cqr_static"} — "cqr_static" se uno dei due ACI è in cold start
    (n_updates < ACI_COLD_START_N), segnalato al logger upstream per diagnostica.

    Logica: se ACI è warm, riscala i bound CQR con fattore alpha_target/alpha_t.
    Il fattore è clampato a [MIN_FACTOR, MAX_FACTOR] per evitare correzioni
    patologiche quando alpha_t si avvicina a 0 (drift prolungato). Senza clamp,
    f può arrivare a 10+ e produrre bande inutilmente larghe (es. ±30°C).
    """
    if aci_80.n_updates < ACI_COLD_START_N or aci_90.n_updates < ACI_COLD_START_N:
        return ci80_lo, ci80_hi, ci90_lo, ci90_hi, "cqr_static"

    # Fattore di scala ACI: alpha_target / alpha_t. Clampato a [MIN, MAX] per
    # evitare bande patologiche quando alpha_t si avvicina al clamp eps=0.01.
    f80 = max(ACI_CORRECTION_FACTOR_MIN, min(ACI_CORRECTION_FACTOR_MAX,
                                            aci_80.alpha_target / aci_80.alpha_t))
    f90 = max(ACI_CORRECTION_FACTOR_MIN, min(ACI_CORRECTION_FACTOR_MAX,
                                            aci_90.alpha_target / aci_90.alpha_t))

    w80 = (ci80_hi - ci80_lo) / 2
    w90 = (ci90_hi - ci90_lo) / 2
    c80 = (ci80_hi + ci80_lo) / 2
    c90 = (ci90_hi + ci90_lo) / 2
    return (
        c80 - w80 * f80, c80 + w80 * f80,
        c90 - w90 * f90, c90 + w90 * f90,
        "aci",
    )


def predict(
    artifacts: TrainingArtifacts,
    X: pd.DataFrame,
    lead_h: int = 0,
) -> dict[str, dict[str, Any]]:
    """Genera predizioni con CI CQR-aggiustati per una singola riga.

    Returns:
        {target: {"p05": ..., "p10": ..., "p50": ..., "p90": ..., "p95": ...,
                  "ci80_lo": ..., "ci80_hi": ..., "ci90_lo": ..., "ci90_hi": ...}}
        Il target "rain_clf" ha {"prob_rain": float}.
    """
    X = X.copy()
    for col in artifacts.categorical_cols:
        if col in X.columns:
            X[col] = X[col].astype("category")

    bucket = _lead_time_bucket(lead_h)
    out: dict[str, dict[str, Any]] = {}
    for target, bundle in artifacts.targets.items():
        use_init = target in artifacts.init_score_targets
        preds_q = {f"p{int(q*100):02d}": float(_predict_level(model, X, target, use_init)[0])
                   for q, model in bundle.models.items()}
        # Anti-crossing: i 5 modelli sono indipendenti e i valori possono incrociarsi.
        sorted_vals = sorted(preds_q[k] for k in _Q_ORDER)
        preds_q = dict(zip(_Q_ORDER, sorted_vals, strict=True))
        pred = _apply_cqr(preds_q, bundle, bucket)
        out[target] = pred

    if artifacts.rain_classifier is not None:
        clf = artifacts.rain_classifier
        X_clf = X.copy()
        for col in artifacts.categorical_cols:
            if col in X_clf.columns:
                X_clf[col] = X_clf[col].astype("category")
        prob_raw = np.asarray(clf.model.predict(X_clf))
        prob_cal = _apply_rain_calibration(prob_raw, clf.calibration)
        out["rain_clf"] = {"prob_rain": float(np.clip(prob_cal[0], 0.0, 1.0))}

    return out


def predict_frame(
    artifacts: TrainingArtifacts,
    X: pd.DataFrame,
    lead_h: list[int],
) -> list[dict[str, dict[str, Any]]]:
    """Predice tutte le righe di X in batch — output identico a predict() riga-per-riga.

    Una sola chiamata model.predict per (target, quantile) sull'intero frame invece
    di una per riga: LightGBM è row-independent, quindi i quantili sono identici; la
    correzione CQR resta per-riga in base al bucket di lead_h[i]. Usato dal job predict
    per evitare 15 chiamate-modello per giorno (15 per location, una sola volta).

    Args:
        lead_h: lead time orario per ogni riga di X (stesso ordine, stessa lunghezza).

    Returns:
        Lista di dict per-riga, stesso formato di predict().
    """
    if len(X) != len(lead_h):
        raise ValueError(f"X ha {len(X)} righe ma lead_h ne ha {len(lead_h)}")

    X = X.copy()
    for col in artifacts.categorical_cols:
        if col in X.columns:
            X[col] = X[col].astype("category")

    buckets = [_lead_time_bucket(h) for h in lead_h]
    out: list[dict[str, dict[str, Any]]] = [{} for _ in range(len(X))]

    # Pre-calcola prob_rain per tutto il frame (una sola predict sul Booster).
    prob_cal_all: np.ndarray | None = None
    if artifacts.rain_classifier is not None:
        clf = artifacts.rain_classifier
        X_clf = X.copy()
        for col in artifacts.categorical_cols:
            if col in X_clf.columns:
                X_clf[col] = X_clf[col].astype("category")
        prob_raw_all = np.asarray(clf.model.predict(X_clf))
        prob_cal_all = np.clip(_apply_rain_calibration(prob_raw_all, clf.calibration), 0.0, 1.0)

    for target, bundle in artifacts.targets.items():
        use_init = target in artifacts.init_score_targets
        q_cols = {f"p{int(q*100):02d}": _predict_level(model, X, target, use_init)
                  for q, model in bundle.models.items()}
        for i in range(len(X)):
            preds_q = {k: float(v[i]) for k, v in q_cols.items()}
            # Anti-crossing: ordina i valori predetti prima di passarli a CQR.
            sorted_vals = sorted(preds_q[k] for k in _Q_ORDER)
            preds_q = dict(zip(_Q_ORDER, sorted_vals, strict=True))
            pred = _apply_cqr(preds_q, bundle, buckets[i])
            out[i][target] = pred

    if prob_cal_all is not None:
        for i in range(len(X)):
            out[i]["rain_clf"] = {"prob_rain": float(prob_cal_all[i])}

    return out


# ── Walk-forward CV ───────────────────────────────────────────────────────────

def walk_forward_cv(
    db: DuckDBClient,
    n_splits: int = 4,
    min_train_days: int = 365,
    embargo_days: int = 7,
    cal_fraction: float = 0.15,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk-forward CV temporale con embargo 7 giorni.

    Ogni fold:
      train:  data dalla data più vecchia a test_start - embargo_days
      cal:    ultimi cal_fraction del train set (per CQR)
      test:   finestra di ~fold_size giorni

    Returns:
        Tupla (aggregate_df, per_bucket_df):
          - aggregate_df: 1 riga per (split, target) con metriche aggregate sul test set
          - per_bucket_df: 1 riga per (split, target, lead_bucket) — breakdown per bucket
            di lead_time_h (0-6h, 6-12h, 12-24h, 24-48h, 48-72h, 72h+) per diagnosticare
            la calibrazione CQR per orizzonte.
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
    rows_per_bucket: list[dict[str, Any]] = []

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

        cal_start_ts = pd.Timestamp(cal_start)
        df_fit_fold, df_es_val_fold = _es_val_split(df_train_full, end_date=cal_start_ts)
        has_es_val_fold = len(df_es_val_fold) > 0
        # df_train sostituito da df_fit_fold per il training effettivo
        df_train = df_fit_fold

        logger.info(
            f"Fold {i+1}/{n_splits}: train={len(df_train)} cal={len(df_cal)} "
            f"test={len(df_test)} ({test_start}→{test_end})"
        )

        for target in TARGETS:
            col = _target_col(target)
            nwp_col = _TARGET_NWP_MEAN[target]

            mask_tr = df_train[col].notna()
            X_tr = df_train.loc[mask_tr, FEATURE_COLS]
            y_tr = df_train.loc[mask_tr, col]

            if len(y_tr) < 50:
                logger.warning(f"Fold {i+1} [{target}]: train troppo piccolo ({len(y_tr)}), skip")
                continue

            models_q: dict[float, lgb.LGBMRegressor | lgb.Booster] = {}
            for q in QUANTILES:
                init_tr = _make_init_score(df_train, target, mask_tr)
                init_val_fold = _make_init_score(df_es_val_fold, target, df_es_val_fold[col].notna()) if has_es_val_fold else None
                if has_es_val_fold:
                    mask_val = df_es_val_fold[col].notna()
                    X_val_es = df_es_val_fold.loc[mask_val, FEATURE_COLS]
                    y_val_es = df_es_val_fold.loc[mask_val, col]
                    models_q[q] = train_lgbm(
                        X_tr, y_tr, q,
                        X_val=X_val_es, y_val=y_val_es,
                        init_score=init_tr, init_score_val=init_val_fold,
                    )
                else:
                    models_q[q] = train_lgbm(X_tr, y_tr, q, init_score=init_tr)

            cqr = _compute_cqr(models_q, df_cal[FEATURE_COLS], df_cal[col], df_cal["lead_time_h"], target=target, use_init_score=True)

            # Valuta sul test set
            mask_te = df_test[col].notna()
            if mask_te.sum() == 0:
                continue

            X_te = df_test.loc[mask_te, FEATURE_COLS]
            y_te = df_test.loc[mask_te, col].values

            preds: dict[float, np.ndarray] = {
                q: _predict_level(m, X_te, target, use_init_score=True) for q, m in models_q.items()
            }

            # MAE su mediana
            mae = float(np.mean(np.abs(y_te - preds[0.50])))

            # CRPS
            crps = crps_from_quantiles(y_te, preds)

            # Coverage empirica con CQR stratificata per lead bucket.
            # features_daily ha lead_time_h 0-168h (post multilead backfill): la
            # correzione CQR è specifica per bucket e va applicata per-riga.
            lead_h_te = df_test.loc[mask_te, "lead_time_h"].values
            buckets_te = np.array([_lead_time_bucket(int(h)) for h in lead_h_te])
            ci80_lo = preds[0.10].copy()
            ci80_hi = preds[0.90].copy()
            ci90_lo = preds[0.05].copy()
            ci90_hi = preds[0.95].copy()
            for label, _ in LEAD_BUCKETS.items():
                m_b = buckets_te == label
                if not m_b.any():
                    continue
                corr = cqr[label]
                ci80_lo[m_b] = preds[0.10][m_b] - corr.ci80
                ci80_hi[m_b] = preds[0.90][m_b] + corr.ci80
                ci90_lo[m_b] = preds[0.05][m_b] - corr.ci90
                ci90_hi[m_b] = preds[0.95][m_b] + corr.ci90
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
                "brier":              None,
                "brier_skill":        None,
                "brier_skill_vs_nwp": None,
                "auc":                None,
                "n_test":     int(mask_te.sum()),
            })

            # Breakdown per lead bucket: una riga per bucket presente nel test
            for label, _ in LEAD_BUCKETS.items():
                m_b = buckets_te == label
                n_b = int(m_b.sum())
                if n_b == 0:
                    continue
                y_b = y_te[m_b]
                preds_b_50 = preds[0.50][m_b]
                mae_b = float(np.mean(np.abs(y_b - preds_b_50)))
                cov80_b = float(np.mean((y_b >= ci80_lo[m_b]) & (y_b <= ci80_hi[m_b])))
                cov90_b = float(np.mean((y_b >= ci90_lo[m_b]) & (y_b <= ci90_hi[m_b])))
                nwp_b = nwp_vals[m_b]
                nwp_mask_b = ~np.isnan(nwp_b)
                if nwp_mask_b.sum() > 0:
                    mae_nwp_b = float(np.mean(np.abs(y_b[nwp_mask_b] - nwp_b[nwp_mask_b])))
                    skill_b = 1 - mae_b / mae_nwp_b if mae_nwp_b > 0 else float("nan")
                else:
                    skill_b = float("nan")
                preds_b_dict: dict[float, np.ndarray] = {
                    q: preds[q][m_b] for q in QUANTILES
                }
                crps_b = crps_from_quantiles(y_b, preds_b_dict)
                rows_per_bucket.append({
                    "split":       i + 1,
                    "target":      target,
                    "lead_bucket": label,
                    "mae":         round(mae_b, 3),
                    "crps":        round(crps_b, 3),
                    "coverage_80": round(cov80_b, 3),
                    "coverage_90": round(cov90_b, 3),
                    "skill_mae":   round(skill_b, 3) if not np.isnan(skill_b) else None,
                    "n_test":      n_b,
                })

        # Classificatore binario pioggia/no — metriche Brier/BSS/AUC per fold
        col_precip = "target_precip_mm"
        mask_precip_fit = df_train[col_precip].notna()
        mask_precip_cal = df_cal[col_precip].notna()
        if mask_precip_fit.sum() >= 50:
            rain_clf_fold = _train_rain_classifier(
                X_fit=df_train.loc[mask_precip_fit, FEATURE_COLS],
                y_precip_fit=df_train.loc[mask_precip_fit, col_precip],
                X_cal=df_cal.loc[mask_precip_cal, FEATURE_COLS],
                y_precip_cal=df_cal.loc[mask_precip_cal, col_precip],
                X_val=df_es_val_fold.loc[df_es_val_fold[col_precip].notna(), FEATURE_COLS] if has_es_val_fold else None,
                y_precip_val=df_es_val_fold.loc[df_es_val_fold[col_precip].notna(), col_precip] if has_es_val_fold else None,
            )

            mask_te_clf = df_test[col_precip].notna()
            if mask_te_clf.sum() >= 10:
                X_te_clf = df_test.loc[mask_te_clf, FEATURE_COLS].copy()
                for col in CATEGORICAL_COLS:
                    if col in X_te_clf.columns:
                        X_te_clf[col] = X_te_clf[col].astype("category")
                y_te_bin = (df_test.loc[mask_te_clf, col_precip] > RAIN_THRESHOLD_MM).astype(int).values

                prob_raw = np.asarray(rain_clf_fold.model.predict(X_te_clf))
                prob_cal = _apply_rain_calibration(prob_raw, rain_clf_fold.calibration)
                prob_cal = np.clip(prob_cal, 0.0, 1.0)

                # Brier Score e Brier Skill Score vs climatologia (frazione wet nel train)
                brier = float(np.mean((prob_cal - y_te_bin) ** 2))
                p_clim = float((df_train.loc[mask_precip_fit, col_precip] > RAIN_THRESHOLD_MM).mean())
                brier_clim = float(p_clim * (1 - p_clim))
                bss = float(1.0 - brier / brier_clim) if brier_clim > 0 else float("nan")

                # AUC-ROC (solo se ci sono entrambe le classi nel test set)
                try:
                    from sklearn.metrics import roc_auc_score  # noqa: PLC0415
                    auc = float(roc_auc_score(y_te_bin, prob_cal)) if len(set(y_te_bin)) > 1 else float("nan")
                except Exception:
                    auc = float("nan")

                # Baseline NWP: frazione modelli con nwp_precip_mean > RAIN_THRESHOLD_MM
                nwp_prob = (df_test.loc[mask_te_clf, "nwp_precip_mean"] > RAIN_THRESHOLD_MM).astype(float).values
                brier_nwp = float(np.mean((nwp_prob - y_te_bin) ** 2))
                bss_vs_nwp = float(1.0 - brier / brier_nwp) if brier_nwp > 0 else float("nan")

                rows.append({
                    "split":       i + 1,
                    "test_start":  str(test_start),
                    "test_end":    str(test_end),
                    "target":      "rain_clf",
                    "mae":         None,
                    "crps":        None,
                    "coverage_80": None,
                    "coverage_90": None,
                    "skill_mae":   None,
                    "brier":       round(brier, 4),
                    "brier_skill": round(bss, 4) if not math.isnan(bss) else None,
                    "brier_skill_vs_nwp": round(bss_vs_nwp, 4) if not math.isnan(bss_vs_nwp) else None,
                    "auc":         round(auc, 4) if not math.isnan(auc) else None,
                    "n_test":      int(mask_te_clf.sum()),
                })

    return pd.DataFrame(rows), pd.DataFrame(rows_per_bucket)
