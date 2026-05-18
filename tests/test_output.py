"""Test per output.py: signal bridge, coverage, JSON writer."""

from __future__ import annotations

import json
import math
from datetime import datetime, UTC
from pathlib import Path

import pandas as pd
import pytest

from guazza.indicators import IndicatorResult
from guazza.output import (
    _prob_exceeds,
    build_signals,
    compute_coverage_30d,
    write_location_json,
)
from guazza.storage import DuckDBClient


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.duckdb"
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
        db.ensure_predictions_schema()
    return db_path


@pytest.fixture
def sample_pred() -> dict:
    return {
        "tmin_c": {
            "p05": 5.0, "p10": 6.0, "p50": 10.0, "p90": 14.0, "p95": 15.0,
            "ci80_lo": 5.5, "ci80_hi": 14.5, "ci90_lo": 4.5, "ci90_hi": 15.5,
        },
        "tmax_c": {
            "p05": 15.0, "p10": 16.0, "p50": 22.0, "p90": 26.0, "p95": 28.0,
            "ci80_lo": 15.5, "ci80_hi": 26.5, "ci90_lo": 14.5, "ci90_hi": 28.5,
        },
        "precip_mm": {
            "p05": 0.0, "p10": 0.0, "p50": 0.5, "p90": 3.0, "p95": 5.0,
            "ci80_lo": 0.0, "ci80_hi": 3.5, "ci90_lo": 0.0, "ci90_hi": 6.0,
        },
    }


@pytest.fixture
def sample_row() -> pd.Series:
    """Row da features_daily con dati NWP plausibili (alta umidità, vento basso)."""
    return pd.Series({
        "ecmwf_humidity_pct": 70.0, "icon_humidity_pct":   75.0,
        "icond2_humidity_pct": 72.0, "gfs_humidity_pct":   68.0,
        "arome_humidity_pct": 74.0, "icon2i_humidity_pct": 71.0,
        "ecmwf_wind_ms": 2.0,  "icon_wind_ms":   3.0,
        "icond2_wind_ms": 2.5, "gfs_wind_ms":    1.5,
        "arome_wind_ms": 2.8,  "icon2i_wind_ms": 2.2,
    })


@pytest.fixture
def sample_indicators() -> list[IndicatorResult]:
    return [
        IndicatorResult(
            indicator_id="panni",
            location_id="casa_campi",
            verdict="verde",
            rule_matched="green",
            alpha=1 / 6,
            cost_fn=5.0,
            cost_fp=1.0,
            ts=datetime(2026, 5, 18, 6, 0, 0, tzinfo=UTC),
        ),
        IndicatorResult(
            indicator_id="motorino",
            location_id="casa_campi",
            verdict="giallo",
            rule_matched="yellow",
            alpha=0.25,
            cost_fn=3.0,
            cost_fp=1.0,
            ts=datetime(2026, 5, 18, 6, 0, 0, tzinfo=UTC),
        ),
    ]


# ── _prob_exceeds ────────────────────────────────────────────────────────────

def test_prob_exceeds_above_all() -> None:
    q = {"p05": 1.0, "p10": 2.0, "p50": 5.0, "p90": 8.0, "p95": 10.0}
    p = _prob_exceeds(q, 15.0)
    assert math.isclose(p, 1.0 - 0.95, rel_tol=1e-9)


def test_prob_exceeds_below_all() -> None:
    q = {"p05": 1.0, "p10": 2.0, "p50": 5.0, "p90": 8.0, "p95": 10.0}
    p = _prob_exceeds(q, 0.0)
    assert math.isclose(p, 1.0 - 0.05, rel_tol=1e-9)


def test_prob_exceeds_at_median() -> None:
    q = {"p05": 0.0, "p10": 1.0, "p50": 5.0, "p90": 9.0, "p95": 10.0}
    p = _prob_exceeds(q, 5.0)
    assert math.isclose(p, 0.5, abs_tol=0.01)


def test_prob_exceeds_interpolated() -> None:
    # threshold esatto a p10 → P(X > p10) = 0.90
    q = {"p05": 0.0, "p10": 2.0, "p50": 5.0, "p90": 9.0, "p95": 10.0}
    p = _prob_exceeds(q, 2.0)
    assert math.isclose(p, 0.90, abs_tol=0.01)


def test_prob_exceeds_empty() -> None:
    assert _prob_exceeds({}, 5.0) == 0.5


def test_prob_exceeds_nonnegative() -> None:
    q = {"p05": 0.0, "p10": 0.0, "p50": 0.5, "p90": 3.0, "p95": 5.0}
    # threshold = 0 → P(precip > 0) alta, comunque in [0, 1]
    p = _prob_exceeds(q, 0.0)
    assert 0.0 <= p <= 1.0


# ── build_signals ─────────────────────────────────────────────────────────────

def test_build_signals_required_keys(sample_pred: dict, sample_row: pd.Series) -> None:
    sig = build_signals(sample_pred, sample_row)
    required = {
        "P(precip > 0.2mm)", "P(precip > 3mm)", "P(precip > 5mm/h)",
        "P(Tmin < 2.0°C)", "P(Tmin < 0.0°C)", "Tmin_p10", "T2m_p50",
        "P(wind > 40kmh)", "P(wind < 5kmh)",
        "P(RH > 80%)", "P(RH > 95% AND wind < 3kmh)",
        "level_sir", "pm10_predicted",
    }
    assert required.issubset(sig.keys())


def test_build_signals_probs_in_range(sample_pred: dict, sample_row: pd.Series) -> None:
    sig = build_signals(sample_pred, sample_row)
    for key, val in sig.items():
        if val is not None and key.startswith("P("):
            assert 0.0 <= val <= 1.0, f"{key}={val} fuori [0, 1]"


def test_build_signals_dry_precip_low_prob(sample_pred: dict, sample_row: pd.Series) -> None:
    # sample_pred ha precip p50=0.5mm: P(precip > 3mm) deve essere < 0.5
    sig = build_signals(sample_pred, sample_row)
    assert sig["P(precip > 3mm)"] < 0.5


def test_build_signals_high_tmin_low_frost(sample_pred: dict, sample_row: pd.Series) -> None:
    # tmin p05=5.0 → P(Tmin < 0°C) quasi 0
    sig = build_signals(sample_pred, sample_row)
    assert sig["P(Tmin < 0.0°C)"] < 0.1


def test_build_signals_obs_summary(sample_pred: dict, sample_row: pd.Series) -> None:
    sig = build_signals(sample_pred, sample_row, obs_summary={"level_sir": 1.5, "pm10_predicted": 35.0})
    assert sig["level_sir"] == pytest.approx(1.5)
    assert sig["pm10_predicted"] == pytest.approx(35.0)


def test_build_signals_missing_nwp(sample_pred: dict) -> None:
    row = pd.Series({"ecmwf_wind_ms": None, "icon_wind_ms": float("nan")})
    sig = build_signals(sample_pred, row)
    # Con tutti NaN → _nwp_frac restituisce 0.5 (fallback)
    assert sig["P(wind > 40kmh)"] == pytest.approx(0.5)


# ── compute_coverage_30d ─────────────────────────────────────────────────────

def test_coverage_30d_no_predictions(seeded_db: Path) -> None:
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        cov = compute_coverage_30d(db, "casa_campi")
    assert all(v is None for v in cov.values())


def test_coverage_30d_insufficient_samples(seeded_db: Path) -> None:
    with DuckDBClient(db_path=seeded_db) as db:
        # Inserisce 3 predictions (< min_samples=10)
        db.upsert_predictions([{
            "model_version": "20260517",
            "location_id":   "casa_campi",
            "ts_valid":      datetime(2026, 5, i, 0, 0, 0),
            "lead_time_h":   24,
            "tmin_c": {"p05": 5.0, "p10": 6.0, "p50": 10.0, "p90": 14.0, "p95": 15.0,
                       "ci80_lo": 5.5, "ci80_hi": 14.5, "ci90_lo": 4.5, "ci90_hi": 15.5},
            "tmax_c": {"p05": 15.0, "p10": 16.0, "p50": 20.0, "p90": 26.0, "p95": 28.0,
                       "ci80_lo": 15.0, "ci80_hi": 26.0, "ci90_lo": 14.0, "ci90_hi": 27.0},
            "precip_mm": {"p05": 0.0, "p10": 0.0, "p50": 0.0, "p90": 1.0, "p95": 2.0,
                          "ci80_lo": 0.0, "ci80_hi": 1.5, "ci90_lo": 0.0, "ci90_hi": 3.0},
        } for i in range(1, 4)])
        cov = compute_coverage_30d(db, "casa_campi")
    assert all(v is None for v in cov.values())


def test_coverage_30d_perfect_coverage(seeded_db: Path) -> None:
    from datetime import timedelta, timezone

    with DuckDBClient(db_path=seeded_db) as db:
        # 15 predictions entro i 30 giorni con obs dentro il CI
        now = datetime.now(tz=timezone.utc).replace(tzinfo=None)
        recs = []
        for i in range(15):
            d = now - timedelta(days=i)
            recs.append({
                "model_version": "20260417",
                "location_id":   "casa_campi",
                "ts_valid":      d,
                "lead_time_h":   24,
                "tmin_c": {"p05": 5.0, "p10": 6.0, "p50": 10.0, "p90": 14.0, "p95": 15.0,
                           "ci80_lo": 5.0, "ci80_hi": 14.0, "ci90_lo": 4.0, "ci90_hi": 15.0},
                "tmax_c": {"p05": 15.0, "p10": 16.0, "p50": 20.0, "p90": 26.0, "p95": 28.0,
                           "ci80_lo": 15.0, "ci80_hi": 26.0, "ci90_lo": 14.0, "ci90_hi": 27.0},
                "precip_mm": {"p05": 0.0, "p10": 0.0, "p50": 0.0, "p90": 1.0, "p95": 2.0,
                              "ci80_lo": 0.0, "ci80_hi": 1.5, "ci90_lo": 0.0, "ci90_hi": 3.0},
            })
        db.upsert_predictions(recs)
        # Simula obs dentro CI80 per tutti
        db.execute("""
            UPDATE predictions
            SET tmin_obs = 10.0, tmax_obs = 20.0, precip_obs = 0.0
            WHERE location_id = 'casa_campi'
        """)
        cov = compute_coverage_30d(db, "casa_campi")

    assert cov["tmin_ci80"] == pytest.approx(1.0)
    assert cov["tmin_ci90"] == pytest.approx(1.0)


# ── write_location_json ──────────────────────────────────────────────────────

def _make_days(
    pred: dict,
    indicators: list[IndicatorResult],
    dates: list[tuple[str, int]] | None = None,
) -> list[dict]:
    """Helper: costruisce una lista day_entries per write_location_json."""
    if dates is None:
        dates = [("2026-05-19", 24)]
    return [
        {"target_date": d, "lead_time_h": lt, "pred": pred, "indicators": indicators}
        for d, lt in dates
    ]


def test_write_location_json_creates_file(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
) -> None:
    coverage = {
        "tmin_ci80": 0.83, "tmin_ci90": 0.90,
        "tmax_ci80": None, "tmax_ci90": None,
        "precip_ci80": None, "precip_ci90": None,
    }
    path = write_location_json(
        location_id="casa_campi",
        days=_make_days(sample_pred, sample_indicators),
        coverage=coverage,
        output_dir=tmp_path,
    )
    assert path.exists()
    assert path.name == "casa_campi.json"


def test_write_location_json_structure(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
) -> None:
    coverage: dict = {"tmin_ci80": None, "tmin_ci90": None,
                      "tmax_ci80": None, "tmax_ci90": None,
                      "precip_ci80": None, "precip_ci90": None}
    days = _make_days(
        sample_pred, sample_indicators,
        dates=[("2026-05-19", 24), ("2026-05-20", 48)],
    )
    path = write_location_json(
        location_id="lavoro_cosimo",
        days=days,
        coverage=coverage,
        output_dir=tmp_path,
    )
    data = json.loads(path.read_text())

    assert data["location_id"] == "lavoro_cosimo"
    assert "generated_at" in data
    assert data["coverage_empirical_30d"]["tmin_ci80"] is None

    assert len(data["days"]) == 2
    day0 = data["days"][0]
    assert day0["target_date"] == "2026-05-19"
    assert day0["lead_time_h"] == 24

    fc = day0["forecasts"]
    assert "tmin_c" in fc and "tmax_c" in fc and "precip_mm" in fc
    for target in fc.values():
        assert "p50" in target
        assert "ci80_lo" in target and "ci80_hi" in target
        assert "ci90_lo" in target and "ci90_hi" in target

    assert "panni" in day0["indicators"]
    assert day0["indicators"]["panni"]["verdict"] == "verde"
    assert day0["indicators"]["motorino"]["verdict"] == "giallo"


def test_write_location_json_valid_json(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
) -> None:
    coverage: dict = {}
    path = write_location_json(
        location_id="test_loc",
        days=_make_days(sample_pred, sample_indicators),
        coverage=coverage,
        output_dir=tmp_path,
    )
    json.loads(path.read_text())


def test_write_location_json_multiday_order(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
) -> None:
    """I giorni compaiono nel JSON nell'ordine in cui vengono passati."""
    dates = [("2026-05-19", 24), ("2026-05-20", 48), ("2026-05-21", 72)]
    path = write_location_json(
        location_id="casa_cesto",
        days=_make_days(sample_pred, sample_indicators, dates=dates),
        coverage={},
        output_dir=tmp_path,
    )
    data = json.loads(path.read_text())
    assert [d["target_date"] for d in data["days"]] == ["2026-05-19", "2026-05-20", "2026-05-21"]
    assert [d["lead_time_h"] for d in data["days"]] == [24, 48, 72]
