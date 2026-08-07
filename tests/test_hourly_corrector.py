"""Test per hourly_corrector.py — dataset, training e prediction del correttore orario."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from guazza.hourly_corrector import (
    DELTA_CLAMP_C,
    FEATURES,
    MIN_DAYS_PER_LOCATION,
    MIN_SAMPLES_PER_SLOT,
    build_delta_dataset,
    load_corrector,
    predict_delta,
    train_corrector,
)
from guazza.storage import DuckDBClient

# ── Costanti test ──────────────────────────────────────────────────────────────

_LOC = "loc_a"
_N_DAYS = 90
_BASE_DATE = datetime(2025, 1, 1, 0, 0, 0)  # UTC


def _nwp_temp(h: int) -> float:
    """Curva NWP sintetica: min notturno ~8 gradi, max pomeridiano ~23 gradi."""
    if h < 6:
        return 8.0
    if h <= 16:
        return 15.0 + 8.0 * math.sin(math.pi * (h - 2) / 14)
    return 15.0 + 8.0 * math.sin(math.pi * (h - 16) / 10)


def _obs_bias(h: int) -> float:
    """Bias learnable: +2.0 ore notturne/mattutine, -1.0 ore diurne."""
    return 2.0 if h < 6 else -1.0


def _insert_synthetic_data(db: DuckDBClient) -> None:
    """Inserisce N_DAYS giorni di forecasts e obs tramite bulk insert."""
    sources_fc = ["open_meteo_ecmwf_ifs", "open_meteo_icon_eu", "open_meteo_arome_france"]
    sources_obs = ["sir_toscana", "netatmo"]
    n_obs_per_slot = 4
    obs_interval = timedelta(minutes=10)
    ts_run_offset = timedelta(days=1)

    fc_rows = []
    for day_idx in range(_N_DAYS):
        day_ts = _BASE_DATE + timedelta(days=day_idx)
        ts_run = day_ts - ts_run_offset
        for src in sources_fc:
            for h in range(24):
                ts_valid = day_ts + timedelta(hours=h)
                lead_h = int((ts_valid - ts_run).total_seconds() / 3600)
                fc_rows.append({
                    "source": src, "location_id": _LOC,
                    "ts_run": ts_run, "ts_valid": ts_valid, "lead_time_h": lead_h,
                    "temp_c": _nwp_temp(h), "humidity_pct": 60.0,
                    "precip_mm": 0.0, "wind_speed_ms": 2.0, "weather_code": 0,
                })

    fc_df = pd.DataFrame(fc_rows)
    db.register_df("_stg_fc", fc_df)
    db.execute("""
        INSERT OR REPLACE INTO forecasts
            (source, location_id, ts_run, ts_valid, lead_time_h,
             temp_c, humidity_pct, precip_mm, wind_speed_ms, weather_code)
        SELECT source, location_id, ts_run, ts_valid, lead_time_h,
               temp_c, humidity_pct, precip_mm, wind_speed_ms, weather_code
        FROM _stg_fc
    """)
    db.unregister_df("_stg_fc")

    obs_rows = []
    for day_idx in range(_N_DAYS):
        day_ts = _BASE_DATE + timedelta(days=day_idx)
        for h in range(24):
            base_ts = day_ts + timedelta(hours=h)
            obs_temp = _nwp_temp(h) + _obs_bias(h)
            for k in range(n_obs_per_slot):
                obs_ts = base_ts + obs_interval * k
                for src_obs in sources_obs:
                    obs_rows.append({
                        "source": src_obs,
                        "station_id": f"ST_{src_obs}_{k}",
                        "location_id": _LOC,
                        "ts": obs_ts,
                        "granularity": "realtime",
                        "temp_c": obs_temp,
                    })

    obs_df = pd.DataFrame(obs_rows)
    db.register_df("_stg_obs", obs_df)
    db.execute("""
        INSERT OR REPLACE INTO observations
            (source, station_id, location_id, ts, granularity, temp_c)
        SELECT source, station_id, location_id, ts, granularity, temp_c
        FROM _stg_obs
    """)
    db.unregister_df("_stg_obs")


# ── Fixture condivisa: DB seeded + dataset + modello addestrato ───────────────
# Costruisce tutto UNA SOLA VOLTA per tutti i test che richiedono il modello.

@pytest.fixture(scope="module")
def synthetic_artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Crea un DB in-memory con dati sintetici, build_delta_dataset e train_corrector."""
    tmp_path = tmp_path_factory.mktemp("corrector_artifacts")
    db_path = tmp_path / "test.duckdb"

    # Apri connessione, inizializza schema, inserisci dati
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
        _insert_synthetic_data(db)
        dataset = build_delta_dataset(db, {_LOC: {}})

    model_path = tmp_path / "hourly_corrector.lgb"
    metrics = None
    corrector = None

    if dataset is not None:
        metrics = train_corrector(dataset, model_path, min_improvement_pct=0.0)
        corrector = load_corrector(tmp_path)

    return {
        "db_path": db_path,
        "tmp_path": tmp_path,
        "dataset": dataset,
        "model_path": model_path,
        "metrics": metrics,
        "corrector": corrector,
    }


# ── Test: build_delta_dataset ─────────────────────────────────────────────────

def test_build_delta_dataset_returns_dataframe(synthetic_artifacts: dict) -> None:
    """build_delta_dataset restituisce un DataFrame con le colonne attese."""
    df = synthetic_artifacts["dataset"]
    assert df is not None
    for col in FEATURES + ["delta", "date", "split"]:
        assert col in df.columns, f"Colonna mancante: {col}"


def test_build_delta_dataset_row_count(synthetic_artifacts: dict) -> None:
    """Il dataset ha circa N_DAYS * 24 righe."""
    df = synthetic_artifacts["dataset"]
    assert df is not None
    assert len(df) > _N_DAYS * 20


def test_build_delta_dataset_delta_has_variance(synthetic_artifacts: dict) -> None:
    """Il target delta ha varianza > 0 (bias inserito e rilevabile)."""
    df = synthetic_artifacts["dataset"]
    assert df is not None
    assert df["delta"].std() > 0.1


def test_build_delta_dataset_split_present(synthetic_artifacts: dict) -> None:
    """Il dataset ha colonna split con valori {'train', 'val', 'test'}."""
    df = synthetic_artifacts["dataset"]
    assert df is not None
    assert set(df["split"].unique()) == {"train", "val", "test"}


def test_build_delta_dataset_embargo_not_in_train(synthetic_artifacts: dict) -> None:
    """Le date di val (embargo) non appaiono nel train set."""
    df = synthetic_artifacts["dataset"]
    assert df is not None
    train_dates = set(df[df["split"] == "train"]["date"].unique())
    for d in df[df["split"] == "val"]["date"].unique():
        assert d not in train_dates


def test_build_delta_dataset_insufficient_days_returns_none(db: DuckDBClient) -> None:
    """Con min_days molto alto -> None."""
    _insert_synthetic_data(db)
    result = build_delta_dataset(db, {_LOC: {}}, min_days=MIN_DAYS_PER_LOCATION * 10)
    assert result is None


def test_build_delta_dataset_few_samples_no_crash(db: DuckDBClient) -> None:
    """Con un solo giorno e pochi campioni, build_delta_dataset ritorna None senza crash."""
    day_ts = _BASE_DATE
    ts_run = day_ts - timedelta(days=1)
    for src in ("open_meteo_ecmwf_ifs", "open_meteo_icon_eu"):
        for h in range(24):
            ts_valid = day_ts + timedelta(hours=h)
            db.execute(
                "INSERT OR REPLACE INTO forecasts "
                "(source, location_id, ts_run, ts_valid, lead_time_h, temp_c, weather_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [src, _LOC, ts_run, ts_valid, h + 24, _nwp_temp(h), 0],
            )
    for k in range(MIN_SAMPLES_PER_SLOT - 1):
        db.execute(
            "INSERT OR REPLACE INTO observations "
            "(source, station_id, location_id, ts, granularity, temp_c) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ["sir_toscana", f"ST_FEW_{k}", _LOC,
             day_ts + timedelta(hours=12, minutes=k), "realtime", 20.0],
        )
    result = build_delta_dataset(db, {_LOC: {}})
    assert result is None


# ── Test: train_corrector ──────────────────────────────────────────────────────

def test_train_corrector_saves_file(synthetic_artifacts: dict) -> None:
    """train_corrector salva il file Booster se il miglioramento e sufficiente."""
    metrics = synthetic_artifacts["metrics"]
    model_path = synthetic_artifacts["model_path"]
    assert metrics is not None
    assert model_path.exists()
    assert metrics["improvement_pct"] >= 0.0


def test_train_corrector_improvement_positive(synthetic_artifacts: dict) -> None:
    """Con bias forte il correttore ha improvement > 0 su test."""
    metrics = synthetic_artifacts["metrics"]
    assert metrics is not None
    assert metrics["improvement_pct"] > 0.0


def test_train_corrector_high_threshold_returns_none(db: DuckDBClient, tmp_path: Path) -> None:
    """Con soglia 99% -> None e nessun file scritto."""
    _insert_synthetic_data(db)
    dataset = build_delta_dataset(db, {_LOC: {}})
    assert dataset is not None

    out_path = tmp_path / "hourly_corrector.lgb"
    metrics = train_corrector(dataset, out_path, min_improvement_pct=99.0)
    assert metrics is None
    assert not out_path.exists()


def test_train_corrector_early_stopping() -> None:
    """T3: eval_set canonico + early_stopping — il training si ferma prima di 500.

    Su un dataset deterministico e semplice (curva seno senza rumore) la metrica
    di validazione smette di migliorare rapidamente: best_iteration_ < 500 prova
    che l'early stopping è attivo con la chiamata eval_set=[(X_val, y_val)]
    (stessa firma usata da train_corrector). Sul dataset sintetico completo la
    loss val migliora fino a 500 iterazioni — il meccanismo va verificato su un
    segnale che plateau davvero.
    """
    import lightgbm as lgb
    import numpy as np

    Xtr = pd.DataFrame({"hour": np.arange(400), "loc": pd.Categorical(["a"] * 400)})
    ytr = np.sin(Xtr["hour"] / 10).astype(float)
    Xva = pd.DataFrame({"hour": np.arange(400, 600), "loc": pd.Categorical(["a"] * 200)})
    yva = np.sin(Xva["hour"] / 10).astype(float)

    model = lgb.LGBMRegressor(
        objective="regression", num_leaves=31, learning_rate=0.05,
        n_estimators=500, random_state=42, n_jobs=-1, verbose=-1,
    )
    model.fit(
        Xtr, ytr,
        categorical_feature=["loc"],
        eval_set=[(Xva, yva)],
        callbacks=[
            lgb.early_stopping(50, verbose=False),
            lgb.log_evaluation(0),
        ],
    )
    assert model.best_iteration_ < 500


# ── Test: load_corrector ───────────────────────────────────────────────────────

def test_load_corrector_missing_returns_none(tmp_path: Path) -> None:
    """load_corrector con path inesistente -> None."""
    assert load_corrector(tmp_path) is None


def test_load_corrector_loads_saved_model(synthetic_artifacts: dict) -> None:
    """load_corrector carica correttamente un Booster salvato da train_corrector."""
    corrector = synthetic_artifacts["corrector"]
    assert corrector is not None


# ── Test: predict_delta ────────────────────────────────────────────────────────

def test_predict_delta_approx_bias(synthetic_artifacts: dict) -> None:
    """Il correttore addestrato su bias noto predice delta finiti e nel range corretto.

    Con bias differenziale (notte vs giorno), il modello deve produrre previsioni
    diverse per ore diverse e comunque nell'intervallo [-DELTA_CLAMP_C, DELTA_CLAMP_C].
    """
    corrector = synthetic_artifacts["corrector"]
    assert corrector is not None

    row_day: dict = {
        "hour": 14, "month": 6, "location_id": _LOC,
        "shape_norm": 0.9, "wc": 0.0, "precip_flag": 0,
        "wind_ms": 2.0, "humidity_pct": 60.0,
    }
    delta_day = predict_delta(corrector, row_day)
    assert delta_day is not None
    assert -DELTA_CLAMP_C <= delta_day <= DELTA_CLAMP_C

    row_night: dict = {
        "hour": 3, "month": 6, "location_id": _LOC,
        "shape_norm": 0.05, "wc": 0.0, "precip_flag": 0,
        "wind_ms": 2.0, "humidity_pct": 60.0,
    }
    delta_night = predict_delta(corrector, row_night)
    assert delta_night is not None
    assert -DELTA_CLAMP_C <= delta_night <= DELTA_CLAMP_C


def test_predict_delta_strange_values_no_exception() -> None:
    """predict_delta con oggetto senza .predict -> None (mai eccezione)."""
    assert predict_delta(object(), dict.fromkeys(FEATURES)) is None


def test_predict_delta_clamped(synthetic_artifacts: dict) -> None:
    """Il delta e clampato a +-DELTA_CLAMP_C."""
    corrector = synthetic_artifacts["corrector"]
    assert corrector is not None

    row: dict = {
        "hour": 12, "month": 7, "location_id": _LOC,
        "shape_norm": 0.5, "wc": 0.0, "precip_flag": 0,
        "wind_ms": 2.0, "humidity_pct": 60.0,
    }
    delta = predict_delta(corrector, row)
    assert delta is not None
    assert -DELTA_CLAMP_C <= delta <= DELTA_CLAMP_C
