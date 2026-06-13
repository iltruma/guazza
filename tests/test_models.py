"""Test per models.py — LightGBM quantile regression + CQR."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from guazza.models import (
    QUANTILES,
    TARGETS,
    TrainingArtifacts,
    _cqr_q_hat,
    _lead_time_bucket,
    crps_from_quantiles,
    load_artifacts,
    predict,
    predict_frame,
    train_all,
    walk_forward_cv,
)
from guazza.storage import DuckDBClient

_CREATE_FEATURES_DAILY = """
CREATE TABLE IF NOT EXISTS features_daily (
    location_id VARCHAR, target_date DATE, lead_time_h BIGINT,
    ecmwf_tmin_c DOUBLE, ecmwf_tmax_c DOUBLE, ecmwf_precip_mm DOUBLE,
    ecmwf_humidity_pct DOUBLE, ecmwf_wind_ms DOUBLE,
    icon_tmin_c DOUBLE, icon_tmax_c DOUBLE, icon_precip_mm DOUBLE,
    icon_humidity_pct DOUBLE, icon_wind_ms DOUBLE,
    icond2_tmin_c DOUBLE, icond2_tmax_c DOUBLE, icond2_precip_mm DOUBLE,
    icond2_humidity_pct DOUBLE, icond2_wind_ms DOUBLE,
    gfs_tmin_c DOUBLE, gfs_tmax_c DOUBLE, gfs_precip_mm DOUBLE,
    gfs_humidity_pct DOUBLE, gfs_wind_ms DOUBLE,
    arome_tmin_c DOUBLE, arome_tmax_c DOUBLE, arome_precip_mm DOUBLE,
    arome_humidity_pct DOUBLE, arome_wind_ms DOUBLE,
    icon2i_tmin_c DOUBLE, icon2i_tmax_c DOUBLE, icon2i_precip_mm DOUBLE,
    icon2i_humidity_pct DOUBLE, icon2i_wind_ms DOUBLE,
    nwp_tmin_mean DOUBLE, nwp_tmin_spread DOUBLE,
    nwp_tmax_mean DOUBLE, nwp_tmax_spread DOUBLE,
    nwp_precip_mean DOUBLE, nwp_precip_spread DOUBLE,
    obs_tmin_c DOUBLE, obs_tmax_c DOUBLE, obs_precip_mm DOUBLE, obs_humidity_pct DOUBLE,
    ring1_precip_d1_mean DOUBLE, ring1_precip_d1_max DOUBLE,
    ring2_precip_d1_mean DOUBLE, ring2_precip_d1_max DOUBLE,
    ring3_precip_d1_mean DOUBLE, ring3_precip_d1_max DOUBLE,
    clim_tmin_mean DOUBLE, clim_tmin_std DOUBLE,
    clim_tmax_mean DOUBLE, clim_tmax_std DOUBLE,
    clim_precip_mean DOUBLE, clim_precip_std DOUBLE,
    month BIGINT, day_of_year BIGINT,
    target_tmin_c DOUBLE, target_tmax_c DOUBLE, target_precip_mm DOUBLE
)
"""


@pytest.fixture(autouse=True)
def fast_lgbm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Riduce n_estimators a 50 per velocizzare il training nei test."""
    from typing import Any

    import guazza.models as _m

    def _fast_params(quantile: float) -> dict[str, Any]:
        return {
            "objective": "quantile",
            "alpha": quantile,
            "metric": "quantile",
            "n_estimators": 50,
            "learning_rate": 0.05,
            "num_leaves": 15,
            "min_child_samples": 10,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "reg_alpha": 0.1,
            "reg_lambda": 0.1,
            "random_state": 42,
            "n_jobs": -1,
            "verbose": -1,
        }

    monkeypatch.setattr(_m, "_lgbm_params", _fast_params)


@pytest.fixture
def db(tmp_path: Path) -> DuckDBClient:
    client = DuckDBClient(db_path=tmp_path / "test.duckdb")
    client.__enter__()
    client.init_schema()
    client.execute(_CREATE_FEATURES_DAILY)
    return client


def _insert_features(db: DuckDBClient, n_days: int = 400, n_locations: int = 2) -> None:
    """Inserisce righe sintetiche in features_daily per il testing."""
    rng = np.random.default_rng(42)
    base = date(2022, 1, 1)

    rows = []
    for loc_idx in range(n_locations):
        loc = f"loc{loc_idx}"
        for i in range(n_days):
            d = base + timedelta(days=i)
            tmin = 5.0 + 10 * np.sin(2 * np.pi * i / 365) + rng.normal(0, 1)
            tmax = tmin + 8 + rng.normal(0, 0.5)
            precip = max(0.0, rng.exponential(1.5) if rng.random() < 0.3 else 0.0)
            rows.append((
                loc, d, 0,
                tmin + rng.normal(0, 0.3), tmax + rng.normal(0, 0.3), precip,
                70.0, 3.0,
                tmin + rng.normal(0, 0.5), tmax + rng.normal(0, 0.5), precip,
                70.0, 3.0,
                tmin + rng.normal(0, 0.4), tmax + rng.normal(0, 0.4), precip,
                70.0, 3.0,
                tmin + rng.normal(0, 0.6), tmax + rng.normal(0, 0.6), precip,
                70.0, 3.0,
                tmin + rng.normal(0, 0.7), tmax + rng.normal(0, 0.7), precip,
                70.0, 3.0,
                tmin + rng.normal(0, 0.4), tmax + rng.normal(0, 0.4), precip,
                70.0, 3.0,
                # ensemble stats
                tmin + rng.normal(0, 0.1), 1.0,
                tmax + rng.normal(0, 0.1), 1.0,
                precip, 0.5,
                # obs yesterday
                tmin - 0.5, tmax - 0.5, precip, 68.0,
                # ring features (NULL — nessuna stazione upstream nei test sintetici)
                None, None, None, None, None, None,
                # climatology
                tmin, 2.0, tmax, 2.0, 1.5, 0.8,
                # calendar
                d.month, d.timetuple().tm_yday,
                # target
                tmin, tmax, precip,
            ))

    db._conn.executemany("""
        INSERT INTO features_daily VALUES (
            ?,?,?,
            ?,?,?,?,?,  ?,?,?,?,?,  ?,?,?,?,?,  ?,?,?,?,?,  ?,?,?,?,?,  ?,?,?,?,?,
            ?,?,?,?,?,?,
            ?,?,?,?,
            ?,?,?,?,?,?,
            ?,?,
            ?,?,?,?,?,?,
            ?,?,?
        )
    """, rows)


def test_lead_time_bucket() -> None:
    assert _lead_time_bucket(0) == "0-6h"
    assert _lead_time_bucket(5) == "0-6h"
    assert _lead_time_bucket(6) == "6-12h"
    assert _lead_time_bucket(24) == "24-48h"
    assert _lead_time_bucket(100) == "72h+"


def test_cqr_q_hat_coverage() -> None:
    rng = np.random.default_rng(0)
    y = rng.normal(0, 1, 200)
    q_lo = y - 1.5
    q_hi = y + 1.5
    q_hat = _cqr_q_hat(q_lo, q_hi, y, alpha=0.10, n=len(y))
    # Con intervallo centrato, q_hat dovrebbe essere ≤ 0 (CI già coperto)
    assert isinstance(q_hat, float)


def test_crps_from_quantiles_perfect() -> None:
    y = np.array([10.0, 20.0, 30.0])
    preds = dict.fromkeys(QUANTILES, y)  # predizioni perfette
    crps = crps_from_quantiles(y, preds)
    assert crps == pytest.approx(0.0, abs=1e-6)


def test_crps_from_quantiles_positive() -> None:
    y = np.array([10.0, 20.0, 30.0])
    preds = dict.fromkeys(QUANTILES, y + 5.0)  # bias costante
    crps = crps_from_quantiles(y, preds)
    assert crps > 0


def test_train_all_returns_artifacts(db: DuckDBClient, tmp_path: Path) -> None:
    _insert_features(db, n_days=400, n_locations=2)
    model_dir = tmp_path / "models"

    artifacts = train_all(db, model_dir=model_dir, cal_days=60)

    assert isinstance(artifacts, TrainingArtifacts)
    assert set(artifacts.targets.keys()) == set(TARGETS)
    assert artifacts.n_train > 0
    assert artifacts.n_cal > 0
    assert (model_dir / "artifacts.json").exists()
    assert (model_dir / "tmin_c_q50.txt").exists()


def test_train_all_models_have_all_quantiles(db: DuckDBClient, tmp_path: Path) -> None:
    _insert_features(db, n_days=400, n_locations=2)
    artifacts = train_all(db, model_dir=tmp_path / "m", cal_days=60)

    for target, bundle in artifacts.targets.items():
        assert set(bundle.models.keys()) == set(QUANTILES), f"{target}: quantili mancanti"
        assert "0-6h" in bundle.cqr, f"{target}: bucket 0-6h mancante"


def test_load_artifacts_roundtrip(db: DuckDBClient, tmp_path: Path) -> None:
    _insert_features(db, n_days=400)
    model_dir = tmp_path / "models"
    trained = train_all(db, model_dir=model_dir, cal_days=60)

    loaded = load_artifacts(model_dir)
    assert isinstance(loaded, TrainingArtifacts)
    assert set(loaded.targets.keys()) == set(TARGETS)
    assert loaded.feature_cols == trained.feature_cols
    assert loaded.targets["tmin_c"].cqr.keys() == trained.targets["tmin_c"].cqr.keys()

    # I Booster ricostruiti devono predire come i regressor in memoria
    # (stesso modello serializzato → stessa mediana).
    from guazza.models import FEATURE_COLS
    X = db.execute("SELECT * FROM features_daily LIMIT 1").df()
    X["location_id"] = X["location_id"].astype("category")
    pred_mem = predict(trained, X[FEATURE_COLS], lead_h=0)
    pred_disk = predict(loaded, X[FEATURE_COLS], lead_h=0)
    for target in TARGETS:
        assert pred_disk[target]["p50"] == pytest.approx(pred_mem[target]["p50"], abs=1e-6)


def test_load_artifacts_rejects_legacy_pickle(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "artifacts.pkl").write_bytes(b"legacy")
    with pytest.raises(RuntimeError, match="pickle"):
        load_artifacts(model_dir)


def test_predict_returns_all_keys(db: DuckDBClient, tmp_path: Path) -> None:
    _insert_features(db, n_days=400)
    artifacts = train_all(db, model_dir=tmp_path / "m", cal_days=60)

    from guazza.models import FEATURE_COLS
    X = db.execute("SELECT * FROM features_daily LIMIT 1").df()
    X["location_id"] = X["location_id"].astype("category")
    X_feat = X[FEATURE_COLS]

    result = predict(artifacts, X_feat, lead_h=0)
    expected_keys = {"p05", "p10", "p50", "p90", "p95", "ci80_lo", "ci80_hi", "ci90_lo", "ci90_hi"}
    for target in TARGETS:
        assert target in result
        assert set(result[target].keys()) == expected_keys


def test_predict_ci_ordering(db: DuckDBClient, tmp_path: Path) -> None:
    _insert_features(db, n_days=400)
    artifacts = train_all(db, model_dir=tmp_path / "m", cal_days=60)

    from guazza.models import FEATURE_COLS
    X = db.execute("SELECT * FROM features_daily LIMIT 5").df()
    X["location_id"] = X["location_id"].astype("category")

    for _, row in X.iterrows():
        result = predict(artifacts, pd.DataFrame([row])[FEATURE_COLS], lead_h=0)
        for target, preds in result.items():
            assert preds["ci90_lo"] <= preds["ci80_lo"], f"{target}: ci90_lo > ci80_lo"
            assert preds["ci80_hi"] <= preds["ci90_hi"], f"{target}: ci80_hi > ci90_hi"
            assert preds["p05"] <= preds["p50"] <= preds["p95"], f"{target}: quantili non ordinati"


def test_predict_frame_matches_per_row(db: DuckDBClient, tmp_path: Path) -> None:
    """predict_frame in batch deve dare lo stesso output di predict() riga-per-riga.

    Invariante alla base dell'ottimizzazione C2 del job predict: include lead time
    diversi (bucket CQR diversi) per coprire la correzione per-riga.
    """
    _insert_features(db, n_days=400)
    artifacts = train_all(db, model_dir=tmp_path / "m", cal_days=60)

    from guazza.models import FEATURE_COLS
    X = db.execute("SELECT * FROM features_daily LIMIT 5").df()
    X["location_id"] = X["location_id"].astype("category")
    X_feat = X[FEATURE_COLS].reset_index(drop=True)
    leads = [0, 24, 48, 120, 168]

    batched = predict_frame(artifacts, X_feat, leads)
    assert len(batched) == len(X_feat)
    for i, lead in enumerate(leads):
        single = predict(artifacts, pd.DataFrame([X_feat.iloc[i]])[FEATURE_COLS], lead_h=lead)
        for target in TARGETS:
            for key, val in single[target].items():
                assert batched[i][target][key] == pytest.approx(val, abs=1e-9)


def test_predict_frame_length_mismatch_raises(db: DuckDBClient, tmp_path: Path) -> None:
    _insert_features(db, n_days=400)
    artifacts = train_all(db, model_dir=tmp_path / "m", cal_days=60)

    from guazza.models import FEATURE_COLS
    X = db.execute("SELECT * FROM features_daily LIMIT 3").df()
    X["location_id"] = X["location_id"].astype("category")
    with pytest.raises(ValueError, match="righe"):
        predict_frame(artifacts, X[FEATURE_COLS], [0, 24])  # 3 righe, 2 lead


def test_walk_forward_cv_returns_dataframe(db: DuckDBClient) -> None:
    _insert_features(db, n_days=600, n_locations=2)
    results = walk_forward_cv(db, n_splits=2, min_train_days=180, embargo_days=7)

    assert isinstance(results, pd.DataFrame)
    assert not results.empty
    expected_cols = {"split", "target", "mae", "crps", "coverage_80", "coverage_90"}
    assert expected_cols.issubset(results.columns)


def test_walk_forward_cv_coverage_reasonable(db: DuckDBClient) -> None:
    _insert_features(db, n_days=800, n_locations=2)
    results = walk_forward_cv(db, n_splits=3, min_train_days=180, embargo_days=7)

    # Verifica che coverage sia un float valido in [0, 1] — non testiamo calibrazione
    # su dati sintetici con modelli veloci (n_estimators ridotto dalla fixture fast_lgbm)
    assert (results["coverage_90"] >= 0.0).all()
    assert (results["coverage_90"] <= 1.0).all()
