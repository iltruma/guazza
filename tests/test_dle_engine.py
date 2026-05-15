"""Test unitari per il Decision Logic Engine."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from guazza.indicators.engine import (
    IndicatorResult,
    SignalBag,
    _compute_alpha,
    _eval_condition,
    evaluate_all,
    evaluate_indicator,
    load_indicators,
    log_results,
)
from guazza.storage.duckdb_client import DuckDBClient


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.duckdb"
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
        db.run_migrations()
    return db_path


# Config minimale per i test — non dipende dal file YAML reale
_CFG_PANNI = {
    "label": "Panni",
    "horizon_h": 8,
    "costs": {"fn": 5, "fp": 1},
    "logic": {
        "green":  "P(precip > 0.2mm) < 0.15 AND P(RH > 80%) < 0.20",
        "yellow": "P(precip > 0.2mm) >= 0.15 AND P(precip > 0.2mm) <= 0.40",
        "red":    "P(precip > 0.2mm) > 0.40",
    },
}

_CFG_BISENZIO = {
    "label": "Bisenzio",
    "horizon_h": 0,
    "location_specific": ["casa_campi"],
    "costs": {"fn": 10, "fp": 1},
    "logic": {
        "green":  "level_sir < threshold_1",
        "yellow": "level_sir >= threshold_1 AND level_sir < threshold_2",
        "red":    "level_sir >= threshold_2",
    },
}

_CFG_MOTORINO = {
    "label": "Motorino",
    "horizon_h": 6,
    "costs": {"fn": 3, "fp": 1},
    "logic": {
        "green":  "P(precip > 0.2mm) < 0.20 AND Tmin_p10 > 4.0",
        "yellow": "P(precip > 0.2mm) >= 0.20 AND P(precip > 0.2mm) <= 0.45",
        "red":    "P(precip > 0.2mm) > 0.45 OR Tmin_p10 < 2.0",
    },
}

_ALL_INDICATORS = {
    "panni":    _CFG_PANNI,
    "bisenzio": _CFG_BISENZIO,
    "motorino": _CFG_MOTORINO,
}


# ── Test _compute_alpha ───────────────────────────────────────────────────────


def test_alpha_panni() -> None:
    """panni: fn=5, fp=1 → alpha = 1/6."""
    alpha = _compute_alpha({"fn": 5, "fp": 1})
    assert math.isclose(alpha, 1 / 6, rel_tol=1e-9)


def test_alpha_symmetric() -> None:
    """Costi uguali → alpha = 0.5."""
    assert math.isclose(_compute_alpha({"fn": 2, "fp": 2}), 0.5)


def test_alpha_bisenzio() -> None:
    """bisenzio: fn=10, fp=1 → alpha ≈ 0.09 (molto conservativo)."""
    alpha = _compute_alpha({"fn": 10, "fp": 1})
    assert math.isclose(alpha, 1 / 11, rel_tol=1e-6)


# ── Test _eval_condition ──────────────────────────────────────────────────────


def test_eval_simple_true() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.05}
    assert _eval_condition("P(precip > 0.2mm) < 0.15", signals) is True


def test_eval_simple_false() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.50}
    assert _eval_condition("P(precip > 0.2mm) < 0.15", signals) is False


def test_eval_and_both_true() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.05, "P(RH > 80%)": 0.10}
    assert _eval_condition("P(precip > 0.2mm) < 0.15 AND P(RH > 80%) < 0.20", signals)


def test_eval_and_one_false() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.05, "P(RH > 80%)": 0.25}
    assert not _eval_condition("P(precip > 0.2mm) < 0.15 AND P(RH > 80%) < 0.20", signals)


def test_eval_direct_variable() -> None:
    """Variabili senza P() (Tmin_p10, level_sir, ecc.) si risolvono direttamente."""
    signals: SignalBag = {"Tmin_p10": 1.5}
    assert _eval_condition("Tmin_p10 < 2.0", signals) is True


def test_eval_missing_signal_defaults_zero() -> None:
    """Segnale mancante → default 0.0 (non eccezione)."""
    assert _eval_condition("P(precip > 0.2mm) < 0.15", {}) is True  # 0.0 < 0.15


def test_eval_or_condition() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.50, "Tmin_p10": 5.0}
    assert _eval_condition("P(precip > 0.2mm) > 0.45 OR Tmin_p10 < 2.0", signals)


# ── Test evaluate_indicator ───────────────────────────────────────────────────


def test_panni_verde() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.05, "P(RH > 80%)": 0.10}
    r = evaluate_indicator("panni", _CFG_PANNI, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "verde"
    assert r.rule_matched == "green"


def test_panni_giallo() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.25, "P(RH > 80%)": 0.10}
    r = evaluate_indicator("panni", _CFG_PANNI, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "giallo"


def test_panni_rosso() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.55, "P(RH > 80%)": 0.10}
    r = evaluate_indicator("panni", _CFG_PANNI, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "rosso"


def test_panni_alpha_correct() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.05, "P(RH > 80%)": 0.10}
    r = evaluate_indicator("panni", _CFG_PANNI, signals, "casa_campi")
    assert r is not None
    assert math.isclose(r.alpha, 1 / 6, rel_tol=1e-9)


def test_bisenzio_location_specific_match() -> None:
    """bisenzio si applica a casa_campi."""
    signals: SignalBag = {"level_sir": 0.5, "threshold_1": 1.2, "threshold_2": 2.0}
    r = evaluate_indicator("bisenzio", _CFG_BISENZIO, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "verde"


def test_bisenzio_location_specific_skip() -> None:
    """bisenzio NON si applica a lavoro_cosimo → None."""
    signals: SignalBag = {"level_sir": 0.5, "threshold_1": 1.2, "threshold_2": 2.0}
    r = evaluate_indicator("bisenzio", _CFG_BISENZIO, signals, "lavoro_cosimo")
    assert r is None


def test_bisenzio_giallo() -> None:
    signals: SignalBag = {"level_sir": 1.5, "threshold_1": 1.2, "threshold_2": 2.0}
    r = evaluate_indicator("bisenzio", _CFG_BISENZIO, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "giallo"


def test_bisenzio_rosso() -> None:
    signals: SignalBag = {"level_sir": 2.5, "threshold_1": 1.2, "threshold_2": 2.0}
    r = evaluate_indicator("bisenzio", _CFG_BISENZIO, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "rosso"


def test_motorino_rosso_freddo() -> None:
    """Tmin_p10 < 2°C → rosso anche con poca pioggia."""
    signals: SignalBag = {"P(precip > 0.2mm)": 0.10, "Tmin_p10": 1.0}
    r = evaluate_indicator("motorino", _CFG_MOTORINO, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "rosso"


# ── Test evaluate_all ─────────────────────────────────────────────────────────


def test_evaluate_all_filters_location_specific() -> None:
    """bisenzio non appare nei risultati per lavoro_cosimo."""
    signals: SignalBag = {
        "P(precip > 0.2mm)": 0.05, "P(RH > 80%)": 0.10,
        "Tmin_p10": 8.0, "level_sir": 0.5,
        "threshold_1": 1.2, "threshold_2": 2.0,
    }
    results = evaluate_all(_ALL_INDICATORS, signals, "lavoro_cosimo")
    ids = {r.indicator_id for r in results}
    assert "bisenzio" not in ids
    assert "panni" in ids
    assert "motorino" in ids


def test_evaluate_all_includes_location_specific() -> None:
    """bisenzio appare nei risultati per casa_campi."""
    signals: SignalBag = {
        "P(precip > 0.2mm)": 0.05, "P(RH > 80%)": 0.10,
        "Tmin_p10": 8.0, "level_sir": 0.5,
        "threshold_1": 1.2, "threshold_2": 2.0,
    }
    results = evaluate_all(_ALL_INDICATORS, signals, "casa_campi")
    ids = {r.indicator_id for r in results}
    assert "bisenzio" in ids


# ── Test log_results ──────────────────────────────────────────────────────────


def test_log_results_inserts(seeded_db: Path) -> None:
    """log_results() salva i record in indicator_log."""
    ts = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    results = [
        IndicatorResult(
            indicator_id="panni",
            location_id="casa_campi",
            verdict="verde",
            rule_matched="green",
            alpha=1 / 6,
            cost_fn=5.0,
            cost_fp=1.0,
            ts=ts,
        )
    ]
    with DuckDBClient(db_path=seeded_db) as db:
        log_results(db, results, input_summary={"P(precip > 0.2mm)": 0.05})
        count = db.execute("SELECT COUNT(*) FROM indicator_log").fetchone()[0]
    assert count == 1


def test_log_results_idempotent(seeded_db: Path) -> None:
    """log_results() due volte con stessa (ts, location, indicator) non duplica."""
    ts = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
    results = [
        IndicatorResult(
            indicator_id="panni",
            location_id="casa_campi",
            verdict="verde",
            rule_matched="green",
            alpha=1 / 6,
            cost_fn=5.0,
            cost_fp=1.0,
            ts=ts,
        )
    ]
    with DuckDBClient(db_path=seeded_db) as db:
        log_results(db, results)
        log_results(db, results)
        count = db.execute("SELECT COUNT(*) FROM indicator_log").fetchone()[0]
    assert count == 1


def test_log_results_empty_no_crash(seeded_db: Path) -> None:
    """log_results() con lista vuota non crasha."""
    with DuckDBClient(db_path=seeded_db) as db:
        log_results(db, [])


# ── Smoke test con config reale ───────────────────────────────────────────────


def test_load_real_config_has_9_indicators() -> None:
    """Il file indicators.yaml reale deve avere esattamente 9 indicatori."""
    try:
        indicators = load_indicators()
    except FileNotFoundError:
        pytest.skip("Config non disponibile in questo ambiente")
    assert len(indicators) == 9
    expected = {"panni", "motorino", "gelata", "temporale", "nebbia",
                "bisenzio", "aria", "annaffia", "clima"}
    assert set(indicators.keys()) == expected


def test_real_config_all_have_costs() -> None:
    """Tutti gli indicatori nel config reale devono avere costs.fn e costs.fp."""
    try:
        indicators = load_indicators()
    except FileNotFoundError:
        pytest.skip("Config non disponibile in questo ambiente")
    for ind_id, cfg in indicators.items():
        assert "costs" in cfg, f"{ind_id} manca costs"
        assert "fn" in cfg["costs"], f"{ind_id} manca costs.fn"
        assert "fp" in cfg["costs"], f"{ind_id} manca costs.fp"


def test_real_config_alpha_range() -> None:
    """alpha deve essere in (0, 1) per tutti gli indicatori."""
    try:
        indicators = load_indicators()
    except FileNotFoundError:
        pytest.skip("Config non disponibile in questo ambiente")
    for ind_id, cfg in indicators.items():
        alpha = _compute_alpha(cfg["costs"])
        assert 0 < alpha < 1, f"{ind_id}: alpha={alpha} fuori range"
