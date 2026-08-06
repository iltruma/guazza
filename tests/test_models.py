"""Test per models.py — LightGBM quantile regression + CQR."""

from __future__ import annotations

from collections.abc import Generator
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from guazza.aci import AdaptiveConformalizer, apply_aci_correction
from guazza.cv import walk_forward_cv

# Schema di test derivato dalle costanti condivise con features.build_features_daily.
# L'ordine delle colonne deve matchare esattamente quello di features_daily in
# produzione, altrimenti l'INSERT in _insert_features inserisce valori nelle
# colonne sbagliate. Se NWP_MODEL_PREFIXES cambia, lo schema di test si adatta.
from guazza.features import NWP_DAILY_VARS, NWP_MODEL_PREFIXES
from guazza.models import (
    FEATURE_COLS,
    QUANTILES,
    RAIN_THRESHOLD_MM,
    TARGETS,
    ClassifierBundle,
    ModelBundle,
    TrainingArtifacts,
    _cqr_q_hat,
    _lead_time_bucket,
    _train_rain_classifier,
    crps_from_quantiles,
    load_artifacts,
    predict,
    predict_frame,
    train_all,
)
from guazza.storage import DuckDBClient

_NWP_FEATURE_COLS = ",\n    ".join(
    f"{prefix}_{var} DOUBLE"
    for prefix, _src in NWP_MODEL_PREFIXES
    for var in NWP_DAILY_VARS
)

_CREATE_FEATURES_DAILY = f"""
CREATE TABLE IF NOT EXISTS features_daily (
    location_id VARCHAR, target_date DATE, lead_time_h BIGINT,
    {_NWP_FEATURE_COLS},
    nwp_tmin_mean DOUBLE, nwp_tmin_spread DOUBLE,
    nwp_tmax_mean DOUBLE, nwp_tmax_spread DOUBLE,
    nwp_precip_mean DOUBLE, nwp_precip_spread DOUBLE,
    nwp_pressure_mean DOUBLE, nwp_pressure_spread DOUBLE,
    nwp_cape_mean DOUBLE, nwp_cape_spread DOUBLE,
    obs_tmin_c DOUBLE, obs_tmax_c DOUBLE, obs_precip_mm DOUBLE, obs_humidity_pct DOUBLE,
    obs_tmin_d2 DOUBLE, obs_tmax_d2 DOUBLE,
    obs_tmin_gradient DOUBLE, obs_tmax_gradient DOUBLE,
    anom_tmin_c DOUBLE, anom_tmax_c DOUBLE,
    clim_tmin_mean DOUBLE, clim_tmin_std DOUBLE,
    clim_tmax_mean DOUBLE, clim_tmax_std DOUBLE,
    clim_precip_mean DOUBLE, clim_precip_std DOUBLE,
    month BIGINT, day_of_year BIGINT,
    doy_sin DOUBLE, doy_cos DOUBLE,
    ring1_precip_d1_mean DOUBLE, ring1_precip_d1_max DOUBLE,
    ring2_precip_d1_mean DOUBLE, ring2_precip_d1_max DOUBLE,
    ring3_precip_d1_mean DOUBLE, ring3_precip_d1_max DOUBLE,
    target_tmin_anom_c DOUBLE, target_tmax_anom_c DOUBLE,
    target_tmin_c DOUBLE, target_tmax_c DOUBLE, target_precip_mm DOUBLE
)
"""


_FAST_LGBM_BASE = {
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


@pytest.fixture(autouse=True)
def fast_lgbm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Riduce n_estimators a 50 per velocizzare il training nei test."""
    import guazza.models as _m

    monkeypatch.setattr(_m, "_lgbm_params",
                        lambda q: {"objective": "quantile", "alpha": q, "metric": "quantile", **_FAST_LGBM_BASE})
    monkeypatch.setattr(_m, "_base_lgbm_params", lambda: dict(_FAST_LGBM_BASE))


@pytest.fixture
def db_with_features(tmp_path: Path) -> Generator[DuckDBClient]:
    with DuckDBClient(db_path=tmp_path / "test.duckdb") as client:
        client.init_schema()
        client.execute(_CREATE_FEATURES_DAILY)
        yield client


@pytest.fixture(scope="module")
def cv_results(tmp_path_factory: pytest.TempPathFactory) -> tuple:
    """Esegue walk_forward_cv una sola volta per il modulo (n_days=250).

    Restituisce (aggregate, per_bucket). Condiviso fra tutti i test CV
    per evitare di riallenare 4 volte lo stesso modello.
    """
    import guazza.models as _m

    orig_lgbm = _m._lgbm_params
    orig_base = _m._base_lgbm_params
    _m._lgbm_params = lambda q: {"objective": "quantile", "alpha": q, "metric": "quantile", **_FAST_LGBM_BASE}
    _m._base_lgbm_params = lambda: dict(_FAST_LGBM_BASE)

    db_path = tmp_path_factory.mktemp("cv_db") / "test.duckdb"
    with DuckDBClient(db_path=db_path) as client:
        client.init_schema()
        client.execute(_CREATE_FEATURES_DAILY)
        _insert_features(client, n_days=250, n_locations=1)
        aggregate, per_bucket = walk_forward_cv(client, n_splits=2, min_train_days=180, embargo_days=7)

    _m._lgbm_params = orig_lgbm
    _m._base_lgbm_params = orig_base
    return aggregate, per_bucket


def _insert_features(db: DuckDBClient, n_days: int = 400, n_locations: int = 2) -> None:
    """Inserisce righe sintetiche in features_daily per il testing.

    L'ordine e il numero di colonne deve matchare `_CREATE_FEATURES_DAILY` (e quindi
    lo schema reale prodotto da `build_features_daily`). NWP e placeholder INSERT
    sono derivati da `NWP_MODEL_PREFIXES × NWP_DAILY_VARS` per restare allineati
    automaticamente a eventuali cambi di modelli.
    """
    import math

    rng = np.random.default_rng(42)
    base = date(2022, 1, 1)

    rows = []
    for loc_idx in range(n_locations):
        loc = f"loc{loc_idx}"
        for i in range(n_days):
            d = base + timedelta(days=i)
            tmin = 5.0 + 10 * np.sin(2 * np.pi * i / 365) + rng.normal(0, 1)
            tmax = tmin + 8 + rng.normal(0, 0.5)
            precip = float(rng.exponential(2.0)) if rng.random() < 0.4 else 0.0
            pressure = 1013.0 + rng.normal(0, 5)
            doy = d.timetuple().tm_yday
            # NWP per modello: derivato da NWP_DAILY_VARS per restare allineato.
            # Valori: tmin, tmax, precip, humidity, wind, pressure_avg, pressure_min
            # (o qualunque ordine NWP_DAILY_VARS definisca in futuro).
            _var_vals = {
                "tmin_c": tmin + rng.normal(0, 0.3),
                "tmax_c": tmax + rng.normal(0, 0.3),
                "precip_mm": precip,
                "humidity_pct": 70.0,
                "wind_ms": 3.0,
                "pressure_hpa_avg": pressure + rng.normal(0, 1),
                "pressure_hpa_min": pressure - 2.0,
                "cape_max": 500.0,
            }
            nwp_values: list[float] = []
            for _prefix, _src in NWP_MODEL_PREFIXES:
                for var in NWP_DAILY_VARS:
                    nwp_values.append(_var_vals[var])
            rows.append((
                loc, d, 0,
                *nwp_values,
                # ensemble stats (tmin/tmax/precip/pressure mean+spread, cape mean+spread)
                tmin + rng.normal(0, 0.1), 1.0,
                tmax + rng.normal(0, 0.1), 1.0,
                precip, 0.5,
                pressure, 3.0,
                500.0, 100.0,
                # obs yesterday
                tmin - 0.5, tmax - 0.5, precip, 68.0,
                # obs lag-2 e gradient
                tmin - 1.0, tmax - 1.0,
                0.5, 0.5,
                # anom (clim_mean == obs nel test, anom=0)
                0.0, 0.0,
                # climatology
                tmin, 2.0, tmax, 2.0, 1.5, 0.8,
                # calendar
                d.month, doy,
                # doy cicliche
                math.sin(2 * math.pi * doy / 365.25),
                math.cos(2 * math.pi * doy / 365.25),
                # ring features (NULL — nessuna stazione upstream nei test sintetici)
                None, None, None, None, None, None,
                # target
                0.0, 0.0,
                tmin, tmax, precip,
            ))

    n_cols = len(rows[0])
    db._conn.executemany(
        "INSERT INTO features_daily VALUES (" + ",".join(["?"] * n_cols) + ")",
        rows,
    )


@pytest.fixture(scope="session")
def trained_artifacts(tmp_path_factory: pytest.TempPathFactory) -> tuple:
    """Allena train_all una volta sola per l'intera sessione di test.

    Restituisce (artifacts, model_dir, db_path) con parametri LightGBM ridotti.
    I test che verificano solo struttura e ordering delle predizioni riusano
    questi artefatti invece di riallenare da zero.

    Nota: session-scope non può dipendere da monkeypatch (function-scope), quindi
    il patch viene fatto direttamente su _m con ripristino manuale.
    """
    import guazza.models as _m

    orig_lgbm = _m._lgbm_params
    orig_base = _m._base_lgbm_params
    _m._lgbm_params = lambda q: {"objective": "quantile", "alpha": q, "metric": "quantile", **_FAST_LGBM_BASE}
    _m._base_lgbm_params = lambda: dict(_FAST_LGBM_BASE)

    model_dir = tmp_path_factory.mktemp("shared_models")
    db_path = tmp_path_factory.mktemp("shared_db") / "test.duckdb"

    with DuckDBClient(db_path=db_path) as client:
        client.init_schema()
        client.execute(_CREATE_FEATURES_DAILY)
        _insert_features(client, n_days=150, n_locations=2)
        artifacts = train_all(client, model_dir=model_dir, cal_days=60)

    _m._lgbm_params = orig_lgbm
    _m._base_lgbm_params = orig_base
    return artifacts, model_dir, db_path


def test_lead_time_bucket() -> None:
    assert _lead_time_bucket(0)   == "D+0"
    assert _lead_time_bucket(24)  == "D+1"
    assert _lead_time_bucket(48)  == "D+2"
    assert _lead_time_bucket(72)  == "D+3"
    assert _lead_time_bucket(96)  == "D+4"
    assert _lead_time_bucket(120) == "D+5+"
    assert _lead_time_bucket(168) == "D+5+"


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


def test_train_all_returns_artifacts(trained_artifacts: tuple) -> None:
    artifacts, model_dir, _ = trained_artifacts

    assert isinstance(artifacts, TrainingArtifacts)
    assert set(artifacts.targets.keys()) == set(TARGETS)
    assert artifacts.n_train > 0
    assert artifacts.n_cal > 0
    assert (model_dir / "artifacts.json").exists()
    assert (model_dir / "tmin_c_q50.txt").exists()


def test_train_all_models_have_all_quantiles(trained_artifacts: tuple) -> None:
    artifacts, _, _ = trained_artifacts

    for target, bundle in artifacts.targets.items():
        assert set(bundle.models.keys()) == set(QUANTILES), f"{target}: quantili mancanti"
        assert "D+0" in bundle.cqr, f"{target}: bucket D+0 mancante"


def test_load_artifacts_roundtrip(trained_artifacts: tuple) -> None:
    trained, model_dir, db_path = trained_artifacts

    loaded = load_artifacts(model_dir)
    assert isinstance(loaded, TrainingArtifacts)
    assert set(loaded.targets.keys()) == set(TARGETS)
    assert loaded.feature_cols == trained.feature_cols
    assert loaded.targets["tmin_c"].cqr.keys() == trained.targets["tmin_c"].cqr.keys()

    # I Booster ricostruiti devono predire come i regressor in memoria
    # (stesso modello serializzato → stessa mediana).
    with DuckDBClient(db_path=db_path, read_only=True) as db:
        X = db.execute("SELECT * FROM features_daily LIMIT 1").df()
    X["location_id"] = X["location_id"].astype("category")
    pred_mem = predict(trained, X[FEATURE_COLS], lead_h=0)
    pred_disk = predict(loaded, X[FEATURE_COLS], lead_h=0)
    for target in TARGETS:
        assert pred_disk[target]["p50"] == pytest.approx(pred_mem[target]["p50"], abs=1e-6)


def test_apply_cqr_enforces_nested_ci() -> None:
    """La CI al 90% deve sempre contenere la CI all'80% (nested CI).

    Caso patologico (precip_mm con cal set zero-inflated): CQR naturale
    produce q_hat_90 < q_hat_80 perché i conformity scores su q05-q95
    (intervallo stretto) sono in media minori di quelli su q10-q90.
    Senza enforcement, ci80_hi > ci90_hi — violazione della proprietà
    teorica del CI nested.
    """
    from guazza.models import CQRCorrection, _apply_cqr

    # Crea un bundle con 5 modelli finti (solo predict, niente LightGBM)
    class FakeModel:
        def __init__(self, q: float) -> None:
            self.q = q
        def predict(self, X: object) -> list[float]:
            return [self.q]
    bundle = ModelBundle(
        models={0.05: FakeModel(0.10), 0.10: FakeModel(0.20),
                0.50: FakeModel(0.50), 0.90: FakeModel(0.90),
                0.95: FakeModel(0.95)},
        cqr={"D+0": CQRCorrection(ci80=0.32, ci90=0.14, n_cal=120)},
    )

    preds_q = {"p05": 0.10, "p10": 0.20, "p50": 0.50, "p90": 1.20, "p95": 1.30}
    out = _apply_cqr(preds_q, bundle, "D+0")
    # Senza enforcement: ci80_hi = 1.20+0.32=1.52, ci90_hi = 1.30+0.14=1.44 (VIOLATO)
    # Con enforcement: ci90_hi = max(1.44, 1.52) = 1.52 (nested CI rispettato)
    assert out["ci80_hi"] == pytest.approx(1.52)
    assert out["ci90_hi"] == pytest.approx(1.52)
    assert out["ci80_hi"] <= out["ci90_hi"]
    # Lato basso: ci80_lo = 0.20-0.32=-0.12, ci90_lo = 0.10-0.14=-0.04
    # Con enforcement: ci90_lo = min(-0.04, -0.12) = -0.12
    assert out["ci80_lo"] == pytest.approx(-0.12)
    assert out["ci90_lo"] == pytest.approx(-0.12)
    assert out["ci90_lo"] <= out["ci80_lo"]


def test_load_artifacts_rejects_legacy_pickle(tmp_path: Path) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "artifacts.pkl").write_bytes(b"legacy")
    with pytest.raises(RuntimeError, match="pickle"):
        load_artifacts(model_dir)


def test_predict_returns_all_keys(trained_artifacts: tuple) -> None:
    artifacts, _, db_path = trained_artifacts

    with DuckDBClient(db_path=db_path, read_only=True) as db:
        X = db.execute("SELECT * FROM features_daily LIMIT 1").df()
    X["location_id"] = X["location_id"].astype("category")

    result = predict(artifacts, X[FEATURE_COLS], lead_h=0)
    expected_keys = {"p05", "p10", "p50", "p90", "p95", "ci80_lo", "ci80_hi", "ci90_lo", "ci90_hi"}
    for target in TARGETS:
        assert target in result
        assert set(result[target].keys()) == expected_keys


def test_predict_ci_ordering(trained_artifacts: tuple) -> None:
    artifacts, _, db_path = trained_artifacts

    with DuckDBClient(db_path=db_path, read_only=True) as db:
        X = db.execute("SELECT * FROM features_daily LIMIT 5").df()
    X["location_id"] = X["location_id"].astype("category")

    for _, row in X.iterrows():
        result = predict(artifacts, pd.DataFrame([row])[FEATURE_COLS], lead_h=0)
        for target, preds in result.items():
            if target == "rain_clf":
                continue
            assert preds["ci90_lo"] <= preds["ci80_lo"], f"{target}: ci90_lo > ci80_lo"
            assert preds["ci80_hi"] <= preds["ci90_hi"], f"{target}: ci80_hi > ci90_hi"
            assert preds["p05"] <= preds["p50"] <= preds["p95"], f"{target}: quantili non ordinati"


def test_predict_frame_matches_per_row(trained_artifacts: tuple) -> None:
    """predict_frame in batch deve dare lo stesso output di predict() riga-per-riga.

    Invariante alla base dell'ottimizzazione C2 del job predict: include lead time
    diversi (bucket CQR diversi) per coprire la correzione per-riga.
    """
    artifacts, _, db_path = trained_artifacts

    with DuckDBClient(db_path=db_path, read_only=True) as db:
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


def test_predict_frame_length_mismatch_raises(trained_artifacts: tuple) -> None:
    artifacts, _, db_path = trained_artifacts

    with DuckDBClient(db_path=db_path, read_only=True) as db:
        X = db.execute("SELECT * FROM features_daily LIMIT 3").df()
    X["location_id"] = X["location_id"].astype("category")
    with pytest.raises(ValueError, match="righe"):
        predict_frame(artifacts, X[FEATURE_COLS], [0, 24])  # 3 righe, 2 lead


def test_walk_forward_cv_structure_and_coverage(cv_results: tuple) -> None:
    """walk_forward_cv restituisce DataFrame con le colonne attese e coverage in [0, 1].

    Accorpa: returns_dataframe, coverage_reasonable, cqr_per_row.
    """
    aggregate, per_bucket = cv_results

    assert isinstance(aggregate, pd.DataFrame) and isinstance(per_bucket, pd.DataFrame)
    assert not aggregate.empty
    expected_cols = {"split", "target", "mae", "crps", "coverage_80", "coverage_90"}
    assert expected_cols.issubset(aggregate.columns)
    # per_bucket: 1 riga per (split, target, lead_bucket)
    assert not per_bucket.empty
    assert {"split", "target", "lead_bucket", "n_test"}.issubset(per_bucket.columns)
    assert (per_bucket["n_test"] > 0).all()

    # Coverage in [0, 1] (non testiamo calibrazione su dati sintetici veloci).
    # Le righe rain_clf hanno coverage_80/90 = None per design → si escludono.
    quant_agg = aggregate[aggregate["target"] != "rain_clf"]
    for df in (quant_agg, per_bucket):
        assert (df["coverage_90"] >= 0.0).all()
        assert (df["coverage_90"] <= 1.0).all()
        assert (df["coverage_80"] >= 0.0).all()
        assert (df["coverage_80"] <= 1.0).all()

    # CQR per-riga: il breakdown per bucket deve esistere con almeno D+0.
    # (Il dataset sintetico ha tutti lead=0 → solo D+0 popolato.)
    assert "D+0" in set(per_bucket["lead_bucket"])
    assert per_bucket["lead_bucket"].nunique() >= 1


# ── Adaptive Conformal Inference (ACI) — spike ─────────────────────────────

def test_aci_starts_at_target() -> None:
    """ACI deve partire da alpha_t = alpha_target."""
    aci = AdaptiveConformalizer(alpha_target=0.10)
    assert aci.alpha_t == 0.10
    assert aci.n_updates == 0


def test_aci_lowers_alpha_under_miscoverage() -> None:
    """Sequenza di miscoverage: ACI abbassa alpha_t → CI più largo (più copertura)."""
    aci = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.05)
    initial = aci.alpha_t

    for _ in range(20):
        aci.update(covered=False)

    # err=1 → alpha_{t+1} = alpha_t + γ*(0.10 - 1) = alpha_t - 0.9*γ
    # alpha_t SCENDE, ma è clampato a eps
    assert aci.alpha_t < initial
    assert aci.alpha_t >= aci.eps
    assert aci.n_updates == 20


def test_aci_raises_alpha_under_over_coverage() -> None:
    """Sequenza di coverage: ACI alza alpha_t → CI più stretto (meno over-coverage)."""
    aci = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.05)
    initial = aci.alpha_t

    for _ in range(20):
        aci.update(covered=True)

    # err=0 → alpha_{t+1} = alpha_t + γ*(0.10 - 0) = alpha_t + 0.1*γ
    # alpha_t SALE, ma è clampato a 1-eps
    assert aci.alpha_t > initial
    assert aci.alpha_t <= 1.0 - aci.eps
    assert aci.n_updates == 20


def test_aci_correct_adjusts_offset() -> None:
    """correct(offset) deve restituire offset più grande se alpha_t < target (CI più largo)."""
    aci = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.05)
    # Forza alpha_t sotto target con miscoverage
    for _ in range(30):
        aci.update(covered=False)
    # alpha_t è sceso, ACI vuole CI più largo
    base_offset = 1.5
    assert aci.correct(base_offset) > base_offset

    # Simmetrico: ACI con over-coverage → offset più stretto
    aci2 = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.05)
    for _ in range(30):
        aci2.update(covered=True)
    assert aci2.correct(base_offset) < base_offset


def test_aci_vs_cqr_static_under_drift() -> None:
    """ACI mantiene copertura long-run al target sotto drift; CQR statico decade.

    Caso sintetico: predizione perfetta, errore N(0, 1) per i primi N/2 sample,
    poi drift a N(+0.5, 1) per i secondi N/2 (modello che cambia).

    CQR statico (calibrato sul primo regime) → coverage decade dopo il drift.
    ACI aggiusta alpha_t online → coverage converge al target.
    """
    rng = np.random.default_rng(42)
    n = 1000
    pred = np.zeros(n)  # predizione perfetta (no model error)
    # Errore reale: regime 1 centrato, regime 2 con bias +0.5
    errors = np.concatenate([
        rng.normal(0.0, 1.0, n // 2),
        rng.normal(0.5, 1.0, n // 2),
    ])
    actuals = pred + errors

    # CQR statico: offset calibrato sul regime 1 → CI [-1.645, +1.645] per α=0.10
    q_hat_static = 1.645
    cov_static_full = (actuals >= -q_hat_static) & (actuals <= q_hat_static)
    cov_static_late = cov_static_full[n // 2:].mean()

    # ACI: alpha_t parte da 0.10, q_hat si aggiusta in base a correct()
    aci = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.02)
    cov_aci = np.zeros(n, dtype=bool)
    for t in range(n):
        q_hat_t = aci.correct(q_hat_static)
        cov_aci[t] = (actuals[t] >= -q_hat_t) & (actuals[t] <= q_hat_t)
        aci.update(cov_aci[t])
    cov_aci_late = cov_aci[n // 2:].mean()

    # CQR statico decade sotto il target 0.90 dopo il drift
    assert cov_static_late < 0.88, (
        f"Pre-condition fallita: CQR statico dovrebbe decadere, late={cov_static_late:.3f}"
    )
    # ACI mantiene copertura significativamente più vicina al target
    assert cov_aci_late > cov_static_late, (
        f"ACI dovrebbe battere CQR statico: aci={cov_aci_late:.3f} static={cov_static_late:.3f}"
    )
    # E deve essere entro 8pp dal target 0.90
    assert abs(cov_aci_late - 0.90) < 0.08, (
        f"ACI late coverage {cov_aci_late:.3f} lontana dal target 0.90"
    )


def test_aci_long_run_coverage_holds() -> None:
    """Proprietà chiave ACI (Gibbs & Candès 2021): su N lungo, copertura empirica → 1-α."""
    rng = np.random.default_rng(0)
    n = 5000
    # Errore sempre N(0, 1) — copertura "teorica" del CQR statico è 90%
    errors = rng.normal(0, 1, n)
    actuals = errors  # pred = 0
    q_hat_static = 1.645
    aci = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.02)
    coverages = np.zeros(n, dtype=bool)
    for t in range(n):
        q_hat_t = aci.correct(q_hat_static)
        coverages[t] = (actuals[t] >= -q_hat_t) & (actuals[t] <= q_hat_t)
        aci.update(coverages[t])

    # Copertura empirica long-run (no drift) deve essere vicina al target 0.90
    # (±3pp tolleranza su N=5000: errore standard ≈ sqrt(0.9*0.1/5000) ≈ 0.004)
    assert abs(coverages.mean() - 0.90) < 0.03


def test_aci_from_state_roundtrip() -> None:
    """from_state deve ricostruire alpha_t e n_updates."""
    aci = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.02)
    for _ in range(50):
        aci.update(covered=False)  # alpha_t scende
    alpha_t_before = aci.alpha_t
    n_updates_before = aci.n_updates
    err_sum_before = aci._err_sum

    reconstructed = AdaptiveConformalizer.from_state(
        alpha_target=0.10,
        alpha_t=alpha_t_before,
        n_updates=n_updates_before,
        err_sum=err_sum_before,
    )
    assert reconstructed.alpha_t == alpha_t_before
    assert reconstructed.n_updates == n_updates_before
    assert reconstructed._err_sum == err_sum_before
    # Deve continuare a funzionare
    new_alpha = reconstructed.update(covered=True)
    assert new_alpha > alpha_t_before  # coverage alza alpha


def test_apply_aci_correction_cold_start_passthrough() -> None:
    """In cold start (n_updates < 30), apply_aci_correction ritorna i bound originali."""
    aci_80 = AdaptiveConformalizer(alpha_target=0.20, learning_rate=0.02)
    aci_90 = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.02)
    # n_updates == 0, freddo
    lo80, hi80, lo90, hi90, source = apply_aci_correction(
        1.0, 9.0, 0.0, 10.0, aci_80, aci_90,
    )
    assert (lo80, hi80, lo90, hi90) == (1.0, 9.0, 0.0, 10.0)
    assert source == "cqr_static"


def test_apply_aci_correction_warm_aci() -> None:
    """ACI warm riscala i bound in base al fattore alpha_target/alpha_t."""
    # Forza alpha_t sotto target con miscoverage
    aci_80 = AdaptiveConformalizer(alpha_target=0.20, learning_rate=0.05)
    aci_90 = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.05)
    for _ in range(50):
        aci_80.update(covered=False)  # alpha_t scende → fattore scala > 1 → CI più largo
        aci_90.update(covered=False)
    assert aci_80.alpha_t < 0.20
    assert aci_90.alpha_t < 0.10

    lo80, hi80, lo90, hi90, source = apply_aci_correction(
        1.0, 9.0, 0.0, 10.0, aci_80, aci_90,
    )
    assert source == "aci"
    # CI 80% originale: width = 8, center = 5
    # ACI allarga → width > 8, center ~ 5
    assert (hi80 - lo80) > 8.0
    assert abs(((hi80 + lo80) / 2) - 5.0) < 0.01
    # CI 90% originale: width = 10, center = 5
    assert (hi90 - lo90) > 10.0


def test_apply_aci_correction_overcoverage_shrinks() -> None:
    """ACI con over-coverage (alpha_t > target) stringe il CI."""
    aci_80 = AdaptiveConformalizer(alpha_target=0.20, learning_rate=0.05)
    aci_90 = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.05)
    for _ in range(50):
        aci_80.update(covered=True)  # alpha_t sale → fattore scala < 1 → CI più stretto
        aci_90.update(covered=True)
    assert aci_80.alpha_t > 0.20
    assert aci_90.alpha_t > 0.10

    lo80, hi80, _, _, source = apply_aci_correction(
        1.0, 9.0, 0.0, 10.0, aci_80, aci_90,
    )
    assert source == "aci"
    assert (hi80 - lo80) < 8.0  # CI stretto


def test_apply_aci_correction_clamps_patological_alpha() -> None:
    """Con alpha_t patologicamente basso (drift prolungato), il fattore di
    correzione è clampato a MAX (2.0) per evitare bande inutilmente larghe.

    Caso reale: alpha_t_80 = 0.018 (vicino al clamp eps=0.01) → senza clamp
    il fattore sarebbe 0.20/0.018 = 11.1, banda 80% 11× la baseline (inutile).
    Con clamp a 2.0, la banda è 2× la baseline (ragionevole).
    """
    aci_80 = AdaptiveConformalizer(alpha_target=0.20, learning_rate=0.05)
    aci_90 = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.05)
    # Forza alpha_t molto basso (simula drift prolungato)
    for _ in range(200):
        aci_80.update(covered=False)  # miscoverage → alpha_t scende
        aci_90.update(covered=False)
    # alpha_t dovrebbe essere vicino a eps=0.01
    assert aci_80.alpha_t < 0.05
    assert aci_90.alpha_t < 0.03

    # CI 80% baseline = (1, 7), width = 6. Con clamp f80=2.0, width raddoppia a 12.
    lo80, hi80, lo90, hi90, source = apply_aci_correction(
        1.0, 7.0, 0.0, 8.0, aci_80, aci_90,
    )
    assert source == "aci"
    # Width clampata a 2× baseline (6 → 12), non 11× (66)
    assert (hi80 - lo80) <= 12.0, f"CI 80% patologica: width={hi80 - lo80}"
    assert (hi90 - lo90) <= 16.0, f"CI 90% patologica: width={hi90 - lo90}"
    # Width comunque >= 1× baseline (ACI non annulla mai la correzione)
    assert (hi80 - lo80) >= 6.0


def test_apply_aci_correction_clamps_min_factor() -> None:
    """Anche con alpha_t molto alto (over-coverage forte), il fattore è
    clampato a MIN (0.5) → la banda non diventa mai meno della metà."""
    aci_80 = AdaptiveConformalizer(alpha_target=0.20, learning_rate=0.05)
    aci_90 = AdaptiveConformalizer(alpha_target=0.10, learning_rate=0.05)
    # Forza alpha_t verso il massimo (1 - eps)
    for _ in range(200):
        aci_80.update(covered=True)
        aci_90.update(covered=True)
    assert aci_80.alpha_t > 0.5
    assert aci_90.alpha_t > 0.5

    lo80, hi80, _, _, _ = apply_aci_correction(
        1.0, 9.0, 0.0, 10.0, aci_80, aci_90,
    )
    # Width baseline 80% = 8, clampata a 0.5×8 = 4 minimo
    assert (hi80 - lo80) >= 4.0
    assert (hi80 - lo80) <= 4.0  # clamp esatto a MIN

def test_get_aci_pair_cold_start(tmp_path: Path) -> None:
    """get_aci_pair senza state in DB deve restituire ACI freschi con alpha_t == alpha_target."""
    from guazza.aci import get_aci_pair

    with DuckDBClient(db_path=tmp_path / "test.duckdb") as db:
        db.init_schema()
        aci_80, aci_90 = get_aci_pair(db, "tmin_c", "0-6h")
    assert aci_80.alpha_t == 0.20
    assert aci_90.alpha_t == 0.10
    assert aci_80.n_updates == 0


def test_get_aci_pair_returns_warm_or_cold(tmp_path: Path) -> None:
    """get_aci_pair restituisce (aci_80, aci_90) coerenti con state DB o fresh."""
    from guazza.aci import get_aci_pair

    with DuckDBClient(db_path=tmp_path / "test.duckdb") as db:
        db.init_schema()
        # Cold start: nessuna state in DB
        aci_80, aci_90 = get_aci_pair(db, "tmin_c", "0-6h")
        assert aci_80.alpha_t == 0.20
        assert aci_90.alpha_t == 0.10
        assert aci_80.n_updates == 0

        # Scrivi state e rileggi
        db.upsert_aci_state("tmin_c", "0-6h",
                            alpha_t_80=0.15, alpha_t_90=0.07,
                            n_updates=50, err_sum_80=10, err_sum_90=5)
        aci_80, aci_90 = get_aci_pair(db, "tmin_c", "0-6h")
        assert aci_80.alpha_t == 0.15
        assert aci_80.n_updates == 50
        assert aci_80._err_sum == 10
        assert aci_90.alpha_t == 0.07
        assert aci_90._err_sum == 5


def test_aci_state_persists_via_duckdb(tmp_path: Path) -> None:
    """upsert_aci_state + get_aci_state roundtrip in DuckDB."""
    with DuckDBClient(db_path=tmp_path / "test.duckdb") as db:
        db.init_schema()

        # Cold: assente
        assert db.get_aci_state("tmin_c", "0-6h") is None

        # Scrivi
        db.upsert_aci_state("tmin_c", "0-6h",
                            alpha_t_80=0.18, alpha_t_90=0.09,
                            n_updates=100, err_sum_80=20, err_sum_90=10)

        # Rileggi
        state = db.get_aci_state("tmin_c", "0-6h")
        assert state is not None
        assert state["alpha_t_80"] == 0.18
        assert state["alpha_t_90"] == 0.09
        assert state["n_updates"] == 100
        assert state["err_sum_80"] == 20
        assert state["err_sum_90"] == 10

        # Update (idempotente): sovrascrive
        db.upsert_aci_state("tmin_c", "0-6h",
                            alpha_t_80=0.16, alpha_t_90=0.08,
                            n_updates=200, err_sum_80=40, err_sum_90=20)
        state2 = db.get_aci_state("tmin_c", "0-6h")
        assert state2 is not None
        assert state2["alpha_t_80"] == 0.16
        assert state2["n_updates"] == 200

        # Bucket diverso: indipendente
        state_other = db.get_aci_state("tmin_c", "24-48h")
        assert state_other is None


def test_load_artifacts_suggests_local_data_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Quando gli artefatti mancano al path di default e data/models esiste,
    l'errore suggerisce --model-dir data/models (UX fix dev locale)."""
    from guazza import models

    # Simula default di produzione + artefatti presenti in data/models.
    fake_prod = tmp_path / "prod"
    fake_local = tmp_path / "data" / "models"
    fake_local.mkdir(parents=True)
    (fake_local / "artifacts.json").write_text("{}")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(models, "_DEFAULT_MODEL_DIR", fake_prod)
    with pytest.raises(FileNotFoundError, match="--model-dir data/models"):
        models.load_artifacts(fake_prod)


def test_load_artifacts_no_hint_outside_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Il suggerimento '--model-dir data/models' appare solo se il path è il default."""
    from guazza import models

    default = tmp_path / "prod"
    other = tmp_path / "custom"
    other.mkdir()
    monkeypatch.setattr(models, "_DEFAULT_MODEL_DIR", default)
    with pytest.raises(FileNotFoundError) as excinfo:
        models.load_artifacts(other)
    # Nessun suggerimento quando il path non è il default di produzione.
    assert "--model-dir" not in str(excinfo.value)


def test_train_rain_classifier_returns_bundle(db_with_features: DuckDBClient, tmp_path: Path) -> None:
    """_train_rain_classifier restituisce un ClassifierBundle con model, threshold e calibrazione."""
    _insert_features(db_with_features, n_days=150, n_locations=1)
    df = db_with_features.execute("SELECT * FROM features_daily").df()
    df["location_id"] = df["location_id"].astype("category")

    mask_fit = df.index < int(len(df) * 0.7)
    mask_cal = ~mask_fit

    X_fit = df.loc[mask_fit, FEATURE_COLS]
    y_fit = df.loc[mask_fit, "target_precip_mm"].fillna(0.0)
    X_cal = df.loc[mask_cal, FEATURE_COLS]
    y_cal = df.loc[mask_cal, "target_precip_mm"].fillna(0.0)

    bundle = _train_rain_classifier(
        X_fit=X_fit,
        y_precip_fit=y_fit,
        X_cal=X_cal,
        y_precip_cal=y_cal,
    )

    assert isinstance(bundle, ClassifierBundle)
    assert bundle.threshold_mm == RAIN_THRESHOLD_MM
    assert bundle.model is not None
    # calibrazione isotonica presente (cal set > 20 righe)
    assert bundle.calibration is not None
    assert len(bundle.calibration.x_thresholds) > 0
    assert len(bundle.calibration.x_thresholds) == len(bundle.calibration.y_calibrated)


def test_predict_emits_prob_rain(trained_artifacts: tuple) -> None:
    """predict() emette out['rain_clf']['prob_rain'] in [0, 1] quando il clf è presente."""
    artifacts, _, db_path = trained_artifacts

    assert artifacts.rain_classifier is not None, "rain_classifier dovrebbe essere presente"

    with DuckDBClient(db_path=db_path, read_only=True) as db:
        df = db.execute("SELECT * FROM features_daily LIMIT 1").df()
    df["location_id"] = df["location_id"].astype("category")

    result = predict(artifacts, df[FEATURE_COLS], lead_h=0)

    assert "rain_clf" in result, "predict() deve emettere rain_clf"
    prob = result["rain_clf"]["prob_rain"]
    assert isinstance(prob, float)
    assert 0.0 <= prob <= 1.0


def test_walk_forward_cv_rain_clf_metrics(cv_results: tuple) -> None:
    """walk_forward_cv deve produrre almeno una riga target='rain_clf' con brier non None."""
    aggregate, _per_bucket = cv_results

    clf_rows = aggregate[aggregate["target"] == "rain_clf"]
    assert not clf_rows.empty, "aggregate_df deve contenere righe con target='rain_clf'"

    # Le righe rain_clf devono avere brier e brier_skill valorizzati
    assert clf_rows["brier"].notna().any(), "brier deve essere non-None in almeno una riga rain_clf"
    assert clf_rows["brier_skill"].notna().any(), "brier_skill deve essere non-None in almeno una riga rain_clf"

    # Le colonne brier/brier_skill/auc devono esistere anche nelle righe dei target quantile
    quant_rows = aggregate[aggregate["target"] != "rain_clf"]
    assert "brier" in quant_rows.columns
    assert quant_rows["brier"].isna().all(), "brier deve essere None per i target quantile"
