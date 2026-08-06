"""Walk-forward cross-validation per la valutazione offline del modello ML.

Usato da script di analisi e dalla suite di test — non fa parte del path
di produzione (forecast/review). La produzione usa train_all() + predict_frame().

Vedi models.py per il codice di training e inference in produzione.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from guazza.models import (
    _TARGET_NWP_MEAN,
    CATEGORICAL_COLS,
    FEATURE_COLS,
    LEAD_BUCKETS,
    QUANTILES,
    RAIN_THRESHOLD_MM,
    TARGETS,
    _apply_rain_calibration,
    _lead_time_bucket,
    _predict_level,
    _target_col,
    _train_quantile_bundle,
    _train_rain_classifier,
    crps_from_quantiles,
    load_features,
)
from guazza.storage import DuckDBClient


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
            di lead_time_h per diagnosticare la calibrazione CQR per orizzonte.
    """
    from guazza.models import _es_val_split  # noqa: PLC0415

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
            if mask_tr.sum() < 50:
                logger.warning(f"Fold {i+1} [{target}]: train troppo piccolo ({mask_tr.sum()}), skip")
                continue

            bundle = _train_quantile_bundle(df_train, df_es_val_fold, df_cal, target)
            models_q = bundle.models
            cqr = bundle.cqr

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
            for label in LEAD_BUCKETS:
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
            for label in LEAD_BUCKETS:
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
