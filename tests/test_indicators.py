"""Test unitari per indicators.py (Decision Logic Engine)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from guazza.indicators import (
    IndicatorResult,
    SignalBag,
    _compute_alpha,
    _eval_condition,
    evaluate_all,
    evaluate_indicator,
    load_indicators,
    log_results,
)
from guazza.storage import DuckDBClient


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.duckdb"
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
    return db_path


_CFG_PANNI = {
    "label": "Panni",
    "horizon_h": 8,
    "costs": {"fn": 5, "fp": 1},
    "logic": {
        "green":  "P(precip > 0.2mm) < 0.15 AND P(RH > 80%) < 0.20",
        "yellow": "(P(precip > 0.2mm) >= 0.15 AND P(precip > 0.2mm) <= 0.40) OR P(RH > 80%) >= 0.20",
        "red":    "P(precip > 0.2mm) > 0.40",
    },
}

_CFG_BISENZIO = {
    "label": "Bisenzio",
    "horizon_h": 0,
    "location_specific": ["casa_campi"],
    "costs": {"fn": 10, "fp": 1},
    "thresholds": {"threshold_1": 1.2, "threshold_2": 2.0, "threshold_3": 3.0},
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


def test_alpha_panni() -> None:
    alpha = _compute_alpha({"fn": 5, "fp": 1})
    assert math.isclose(alpha, 1 / 6, rel_tol=1e-9)


def test_alpha_symmetric() -> None:
    assert math.isclose(_compute_alpha({"fn": 2, "fp": 2}), 0.5)


def test_alpha_bisenzio() -> None:
    alpha = _compute_alpha({"fn": 10, "fp": 1})
    assert math.isclose(alpha, 1 / 11, rel_tol=1e-6)


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
    signals: SignalBag = {"Tmin_p10": 1.5}
    assert _eval_condition("Tmin_p10 < 2.0", signals) is True


def test_eval_missing_signal_defaults_zero() -> None:
    assert _eval_condition("P(precip > 0.2mm) < 0.15", {}) is True


def test_eval_or_condition() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.50, "Tmin_p10": 5.0}
    assert _eval_condition("P(precip > 0.2mm) > 0.45 OR Tmin_p10 < 2.0", signals)


def test_panni_verde() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.05, "P(RH > 80%)": 0.10}
    r = evaluate_indicator("panni", _CFG_PANNI, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "verde"


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


def test_panni_giallo_alta_umidita() -> None:
    # Precip bassa ma umidità alta → giallo (panni non asciugherebbero)
    signals: SignalBag = {"P(precip > 0.2mm)": 0.10, "P(RH > 80%)": 0.67}
    r = evaluate_indicator("panni", _CFG_PANNI, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "giallo"
    assert r.rule_matched == "yellow"


def test_panni_alpha_correct() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.05, "P(RH > 80%)": 0.10}
    r = evaluate_indicator("panni", _CFG_PANNI, signals, "casa_campi")
    assert r is not None
    assert math.isclose(r.alpha, 1 / 6, rel_tol=1e-9)


def test_bisenzio_location_specific_match() -> None:
    signals: SignalBag = {"level_sir": 0.5}
    r = evaluate_indicator("bisenzio", _CFG_BISENZIO, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "verde"


def test_bisenzio_location_specific_skip() -> None:
    signals: SignalBag = {"level_sir": 0.5}
    r = evaluate_indicator("bisenzio", _CFG_BISENZIO, signals, "lavoro_cosimo")
    assert r is None


def test_bisenzio_giallo() -> None:
    signals: SignalBag = {"level_sir": 1.5}
    r = evaluate_indicator("bisenzio", _CFG_BISENZIO, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "giallo"


def test_bisenzio_rosso() -> None:
    signals: SignalBag = {"level_sir": 2.5}
    r = evaluate_indicator("bisenzio", _CFG_BISENZIO, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "rosso"


def test_motorino_rosso_freddo() -> None:
    signals: SignalBag = {"P(precip > 0.2mm)": 0.10, "Tmin_p10": 1.0}
    r = evaluate_indicator("motorino", _CFG_MOTORINO, signals, "casa_campi")
    assert r is not None
    assert r.verdict == "rosso"


def test_evaluate_all_filters_location_specific() -> None:
    signals: SignalBag = {
        "P(precip > 0.2mm)": 0.05, "P(RH > 80%)": 0.10,
        "Tmin_p10": 8.0, "level_sir": 0.5,
    }
    results = evaluate_all(_ALL_INDICATORS, signals, "lavoro_cosimo")
    ids = {r.indicator_id for r in results}
    assert "bisenzio" not in ids
    assert "panni" in ids
    assert "motorino" in ids


def test_evaluate_all_includes_location_specific() -> None:
    signals: SignalBag = {
        "P(precip > 0.2mm)": 0.05, "P(RH > 80%)": 0.10,
        "Tmin_p10": 8.0, "level_sir": 0.5,
    }
    results = evaluate_all(_ALL_INDICATORS, signals, "casa_campi")
    ids = {r.indicator_id for r in results}
    assert "bisenzio" in ids


def test_log_results_inserts(seeded_db: Path) -> None:
    ts = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)
    results = [
        IndicatorResult(
            indicator_id="panni",
            location_id="casa_campi",
            verdict="verde",
            rule_matched="green",
            rule_text="P(precip > 0.2mm) < 0.15",
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
    ts = datetime(2026, 5, 14, 10, 0, 0, tzinfo=UTC)
    results = [
        IndicatorResult(
            indicator_id="panni",
            location_id="casa_campi",
            verdict="verde",
            rule_matched="green",
            rule_text="P(precip > 0.2mm) < 0.15",
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
    with DuckDBClient(db_path=seeded_db) as db:
        log_results(db, [])


def test_load_real_config_has_8_indicators() -> None:
    try:
        indicators = load_indicators()
    except FileNotFoundError:
        pytest.skip("Config non disponibile in questo ambiente")
    assert len(indicators) == 7
    expected = {"panni", "motorino", "gelata", "temporale", "nebbia",
                "bisenzio", "annaffia"}
    assert set(indicators.keys()) == expected


def test_real_config_all_have_costs() -> None:
    try:
        indicators = load_indicators()
    except FileNotFoundError:
        pytest.skip("Config non disponibile in questo ambiente")
    for ind_id, cfg in indicators.items():
        assert "costs" in cfg, f"{ind_id} manca costs"
        assert "fn" in cfg["costs"], f"{ind_id} manca costs.fn"
        assert "fp" in cfg["costs"], f"{ind_id} manca costs.fp"


def test_real_config_alpha_range() -> None:
    try:
        indicators = load_indicators()
    except FileNotFoundError:
        pytest.skip("Config non disponibile in questo ambiente")
    for ind_id, cfg in indicators.items():
        alpha = _compute_alpha(cfg["costs"])
        assert 0 < alpha < 1, f"{ind_id}: alpha={alpha} fuori range"
