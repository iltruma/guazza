"""Test per output.py: signal bridge, coverage, JSON writer."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from guazza.indicators import IndicatorResult
from guazza.output import (
    _WMO_SEVERITY,
    _dewpoint,
    _modal_weather_code,
    _prob_exceeds,
    build_signals,
    build_signals_today,
    compute_coverage_30d,
    compute_hourly_profile,
    expected_precip,
    get_current_air_quality,
    get_current_conditions,
    get_daily_weather_code,
    get_nwp_model_comparison,
    get_nwp_models_hourly,
    refresh_realtime_json,
    write_location_json,
)
from guazza.storage import DuckDBClient

# ── Fixtures ────────────────────────────────────────────────────────────────

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
        "arome_humidity_pct": 74.0, "icon2i_humidity_pct": 71.0,
        "ecmwf_wind_ms": 2.0,  "icon_wind_ms":   3.0,
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
            rule_text="P(precip > 0.2mm) < 0.15",
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
            rule_text="P(precip > 0.2mm) >= 0.15",
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


# ── expected_precip ─────────────────────────────────────────────────────────

def test_expected_precip_uniform() -> None:
    # Distribuzione uniforme su [0, 4]: E[X] = 2.0
    q = {"p05": 0.2, "p10": 0.4, "p50": 2.0, "p90": 3.6, "p95": 3.8}
    ev = expected_precip(q)
    assert ev is not None
    assert math.isclose(ev, 2.0, abs_tol=0.05)


def test_expected_precip_zero_inflated() -> None:
    # Caso tipico: mediana 0, coda pesante a destra → E[X] > p50
    q = {"p05": 0.0, "p10": 0.0, "p50": 0.0, "p90": 3.0, "p95": 5.0}
    ev = expected_precip(q)
    assert ev is not None
    assert ev > 0.0  # E[X] > mediana
    # Con estremi rettangolari: contributo principale da [0.9, 1.0] ≈ 0.1 * 4.0 = 0.4
    assert ev > 0.3


def test_expected_precip_always_ge_zero() -> None:
    # Quantile regression può dare valori leggermente negativi: clamp a 0
    q = {"p05": -0.1, "p10": -0.05, "p50": 0.0, "p90": 0.0, "p95": 0.0}
    ev = expected_precip(q)
    assert ev is not None
    assert ev >= 0.0


def test_expected_precip_missing_quantile() -> None:
    # Quantile mancante → None
    q = {"p05": 0.0, "p10": 0.0, "p50": 0.5}  # mancano p90 e p95
    assert expected_precip(q) is None


def test_expected_precip_in_fmt_precip(sample_pred: dict) -> None:
    # Il campo "mean" deve essere presente nel JSON output e > p50 per distribuzione skewed
    # Usa il sample_pred con precip skewed: p05=0, p10=0, p50=0.5, p90=3.0, p95=5.0
    q = sample_pred["precip_mm"]
    ev = expected_precip(q)
    assert ev is not None
    assert ev > q["p50"]  # distribuzione right-skewed → E[X] > mediana


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


# ── build_signals_today ───────────────────────────────────────────────────────

def test_build_signals_today_no_current_obs_identical(
    sample_pred: dict, sample_row: pd.Series
) -> None:
    """Senza current_obs, build_signals_today == build_signals."""
    sig_base  = build_signals(sample_pred, sample_row)
    sig_today = build_signals_today(sample_pred, sample_row, current_obs=None)
    assert sig_base == sig_today


def test_build_signals_today_precip_deterministic(
    sample_pred: dict, sample_row: pd.Series
) -> None:
    """Con precip_mm osservata >= soglia, probabilità diventa 1.0."""
    obs = {"temp_c": 18.0, "humidity_pct": 70.0, "precip_mm": 4.0, "wind_speed_ms": 2.0}
    sig = build_signals_today(sample_pred, sample_row, current_obs=obs)
    assert sig["P(precip > 0.2mm)"] == pytest.approx(1.0)
    assert sig["P(precip > 3mm)"]   == pytest.approx(1.0)
    assert sig["P(precip > 5mm/h)"] == pytest.approx(0.0)


def test_build_signals_today_no_precip(
    sample_pred: dict, sample_row: pd.Series
) -> None:
    """Con precip_mm = 0, tutte le soglie pioggia a 0."""
    obs = {"temp_c": 20.0, "humidity_pct": 50.0, "precip_mm": 0.0, "wind_speed_ms": 1.0}
    sig = build_signals_today(sample_pred, sample_row, current_obs=obs)
    assert sig["P(precip > 0.2mm)"] == pytest.approx(0.0)
    assert sig["P(precip > 3mm)"]   == pytest.approx(0.0)


def test_build_signals_today_temp_overrides(
    sample_pred: dict, sample_row: pd.Series
) -> None:
    """La temperatura realtime sostituisce T2m_p50."""
    obs = {"temp_c": 7.3, "humidity_pct": 60.0, "precip_mm": 0.0, "wind_speed_ms": 1.0}
    sig = build_signals_today(sample_pred, sample_row, current_obs=obs)
    assert sig["T2m_p50"] == pytest.approx(7.3)


def test_build_signals_today_tmin_from_ml(
    sample_pred: dict, sample_row: pd.Series
) -> None:
    """P(Tmin < 2°C) e Tmin_p10 restano da ML, non si sovrascrivono con realtime."""
    obs = {"temp_c": 25.0, "humidity_pct": 40.0, "precip_mm": 0.0, "wind_speed_ms": 0.5}
    sig_base  = build_signals(sample_pred, sample_row)
    sig_today = build_signals_today(sample_pred, sample_row, current_obs=obs)
    assert sig_today["P(Tmin < 2.0°C)"] == pytest.approx(sig_base["P(Tmin < 2.0°C)"])
    assert sig_today["Tmin_p10"]         == sig_base["Tmin_p10"]


def test_build_signals_today_high_humidity_fog(
    sample_pred: dict, sample_row: pd.Series
) -> None:
    """Umidità > 95% e vento < 3km/h → P(nebbia) = 1.0."""
    obs = {"temp_c": 10.0, "humidity_pct": 97.0, "precip_mm": 0.0, "wind_speed_ms": 0.5}
    sig = build_signals_today(sample_pred, sample_row, current_obs=obs)
    assert sig["P(RH > 80%)"]                  == pytest.approx(1.0)
    assert sig["P(RH > 95% AND wind < 3kmh)"]  == pytest.approx(1.0)


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
    from datetime import timedelta

    with DuckDBClient(db_path=seeded_db) as db:
        # 15 predictions entro i 30 giorni con obs dentro il CI
        now = datetime.now(tz=UTC).replace(tzinfo=None)
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


# ── get_nwp_model_comparison ────────────────────────────────────────────────

def test_nwp_model_comparison_returns_ordered_models(
    seeded_db: Path,
) -> None:
    with DuckDBClient(db_path=seeded_db) as db:
        db.execute("""
            INSERT INTO forecasts
                (source, location_id, ts_run, ts_valid, lead_time_h, temp_c, precip_mm)
            VALUES
                ('open_meteo_ecmwf_ifs', 'casa_campi', '2026-05-18 00:00', '2026-05-19 06:00', 30, 11.0, 0.5),
                ('open_meteo_ecmwf_ifs', 'casa_campi', '2026-05-18 00:00', '2026-05-19 12:00', 36, 18.0, 0.0),
                ('open_meteo_ecmwf_ifs', 'casa_campi', '2026-05-18 00:00', '2026-05-19 18:00', 42, 15.0, 0.2),
                ('open_meteo_icon_eu',   'casa_campi', '2026-05-18 00:00', '2026-05-19 06:00', 30, 10.5, 1.0),
                ('open_meteo_icon_eu',   'casa_campi', '2026-05-18 00:00', '2026-05-19 12:00', 36, 19.5, 0.0)
        """)
        result = get_nwp_model_comparison(db, "casa_campi", "2026-05-19")

    labels = [r["label"] for r in result]
    assert labels == ["ECMWF IFS", "ICON-EU"]

    ecmwf = result[0]
    assert ecmwf["tmin_c"] == pytest.approx(11.0)
    assert ecmwf["tmax_c"] == pytest.approx(18.0)
    assert ecmwf["precip_mm"] == pytest.approx(0.7)


def test_nwp_model_comparison_empty_when_no_data(
    seeded_db: Path,
) -> None:
    with DuckDBClient(db_path=seeded_db) as db:
        result = get_nwp_model_comparison(db, "casa_campi", "2026-05-19")
    assert result == []


def test_nwp_model_comparison_latest_run_wins(
    seeded_db: Path,
) -> None:
    """Il run più recente prevale per lo stesso (source, ts_valid)."""
    with DuckDBClient(db_path=seeded_db) as db:
        db.execute("""
            INSERT INTO forecasts
                (source, location_id, ts_run, ts_valid, lead_time_h, temp_c, precip_mm)
            VALUES
                ('open_meteo_icon_eu', 'casa_campi', '2026-05-18 00:00', '2026-05-19 12:00', 36, 20.0, 0.0),
                ('open_meteo_icon_eu', 'casa_campi', '2026-05-18 06:00', '2026-05-19 12:00', 30, 21.0, 0.0)
        """)
        result = get_nwp_model_comparison(db, "casa_campi", "2026-05-19")

    assert len(result) == 1
    assert result[0]["tmax_c"] == pytest.approx(21.0)  # il run 06:00 vince


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
    # Metadata temporale presente, coerente con generated_at
    assert data["updates"]["pipeline_at"] == data["generated_at"]
    assert data["updates"]["realtime_at"] is None

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


def test_write_location_json_precip_clamp(
    tmp_path: Path,
    sample_indicators: list[IndicatorResult],
) -> None:
    """Valori precip leggermente negativi devono essere clampati a 0 nel JSON."""
    pred_negative = {
        "tmin_c":    {"p50": 10.0, "ci80_lo": 8.0, "ci80_hi": 12.0, "ci90_lo": 7.0, "ci90_hi": 13.0},
        "tmax_c":    {"p50": 20.0, "ci80_lo": 18.0, "ci80_hi": 22.0, "ci90_lo": 17.0, "ci90_hi": 23.0},
        "precip_mm": {"p50": -0.001, "ci80_lo": -0.05, "ci80_hi": 0.2, "ci90_lo": -0.1, "ci90_hi": 0.5},
    }
    path = write_location_json(
        location_id="test_loc",
        days=_make_days(pred_negative, sample_indicators),
        coverage={},
        output_dir=tmp_path,
    )
    data = json.loads(path.read_text())
    fc = data["days"][0]["forecasts"]["precip_mm"]
    assert fc["p50"] == 0.0
    assert fc["ci80_lo"] == 0.0
    assert fc["ci90_lo"] == 0.0
    assert fc["ci80_hi"] == pytest.approx(0.2)


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


# ── compute_hourly_profile ────────────────────────────────────────────────────

def _insert_hourly_nwp(db_path: Path, location_id: str, target_date: str) -> None:
    """Inserisce 24h di NWP fittizio per due sorgenti."""
    from datetime import date

    import duckdb

    d = date.fromisoformat(target_date)
    ts_run = datetime(d.year, d.month, d.day, 0, 0, 0)
    records = []
    for src in ("ecmwf_ifs", "icon_eu"):
        for h in range(24):
            ts_valid = datetime(d.year, d.month, d.day, h, 0, 0)
            lead_time_h = h
            # Temp: parabola con min alle 6, max alle 14; precip solo ore 10-12
            raw_temp = 5.0 + 10.0 * math.sin(math.pi * (h - 6) / 18) if h >= 6 else 5.0
            precip = 2.0 if 10 <= h <= 12 else 0.0
            records.append((src, location_id, ts_run, ts_valid, lead_time_h, raw_temp, precip))

    con = duckdb.connect(str(db_path))
    con.executemany(
        "INSERT OR REPLACE INTO forecasts "
        "(source, location_id, ts_run, ts_valid, lead_time_h, temp_c, precip_mm) "
        "VALUES (?,?,?,?,?,?,?)",
        records,
    )
    con.close()


def test_hourly_profile_no_data(seeded_db: Path) -> None:
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = compute_hourly_profile(db, "casa_campi", "2026-05-19", 5.0, 20.0, 3.0)
    assert result is None


def test_hourly_profile_length(seeded_db: Path) -> None:
    _insert_hourly_nwp(seeded_db, "casa_campi", "2026-05-19")
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = compute_hourly_profile(db, "casa_campi", "2026-05-19", 5.0, 20.0, 3.0)
    assert result is not None
    assert len(result) == 24
    assert [r["hour"] for r in result] == list(range(24))


def test_hourly_profile_temp_anchored(seeded_db: Path) -> None:
    _insert_hourly_nwp(seeded_db, "casa_campi", "2026-05-19")
    tmin, tmax = 4.0, 22.0
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = compute_hourly_profile(db, "casa_campi", "2026-05-19", tmin, tmax, 0.0)
    assert result is not None
    temps = [r["temp_c"] for r in result if r["temp_c"] is not None]
    assert min(temps) == pytest.approx(tmin, abs=0.2)
    assert max(temps) == pytest.approx(tmax, abs=0.2)


def test_hourly_profile_precip_total(seeded_db: Path) -> None:
    _insert_hourly_nwp(seeded_db, "casa_campi", "2026-05-19")
    precip_p50 = 5.0
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = compute_hourly_profile(db, "casa_campi", "2026-05-19", 5.0, 20.0, precip_p50)
    assert result is not None
    total = sum(r["precip_mm"] for r in result if r["precip_mm"] is not None)
    assert total == pytest.approx(precip_p50, rel=0.01)


def test_hourly_profile_precip_zero_when_dry(seeded_db: Path) -> None:
    """Se precip_p50=0, tutte le ore devono essere 0."""
    _insert_hourly_nwp(seeded_db, "casa_campi", "2026-05-19")
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = compute_hourly_profile(db, "casa_campi", "2026-05-19", 5.0, 20.0, 0.0)
    assert result is not None
    # Le ore di bordo del giorno locale (00-01 = 22-23Z del giorno prima) non sono
    # seedate dall'helper e restano None; le ore con dati devono essere tutte secche.
    present = [r["precip_mm"] for r in result if r["precip_mm"] is not None]
    assert present and all(p == 0.0 for p in present)


def test_hourly_profile_in_json(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
    seeded_db: Path,
) -> None:
    """Il campo hourly viene scritto correttamente nel JSON."""
    _insert_hourly_nwp(seeded_db, "casa_campi", "2026-05-19")
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        hourly = compute_hourly_profile(db, "casa_campi", "2026-05-19", 5.0, 20.0, 3.0)
    days = [{"target_date": "2026-05-19", "lead_time_h": 24,
             "pred": sample_pred, "indicators": sample_indicators, "hourly": hourly}]
    path = write_location_json(
        location_id="casa_campi", days=days, coverage={}, output_dir=tmp_path,
    )
    data = json.loads(path.read_text())
    assert data["days"][0]["hourly"] is not None
    assert len(data["days"][0]["hourly"]) == 24


def test_hourly_profile_has_humidity(seeded_db: Path) -> None:
    """humidity_pct è presente nel risultato quando i dati NWP la contengono."""
    from datetime import date

    import duckdb

    d = date(2026, 5, 19)
    ts_run = datetime(d.year, d.month, d.day, 0, 0, 0)
    records = []
    for src in ("ecmwf_ifs", "icon_eu"):
        for h in range(24):
            ts_valid = datetime(d.year, d.month, d.day, h, 0, 0)
            records.append((src, "casa_campi", ts_run, ts_valid, h, 15.0 + h * 0.2, 60.0 + h, 0.0))

    con = duckdb.connect(str(seeded_db))
    con.executemany(
        "INSERT OR REPLACE INTO forecasts "
        "(source, location_id, ts_run, ts_valid, lead_time_h, temp_c, humidity_pct, precip_mm) "
        "VALUES (?,?,?,?,?,?,?,?)",
        records,
    )
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = compute_hourly_profile(db, "casa_campi", "2026-05-19", 5.0, 20.0, 0.0)
    assert result is not None
    hum_values = [r["humidity_pct"] for r in result if r["humidity_pct"] is not None]
    # Giorno locale: il seed copre un solo giorno UTC, quindi le ore 00-01 locali
    # (22-23Z del giorno prima) restano vuote. Ogni ora con dati ha però l'umidità.
    temp_hours = [r for r in result if r["temp_c"] is not None]
    assert hum_values
    assert len(hum_values) == len(temp_hours)
    assert all(h >= 0 for h in hum_values)


def test_hourly_profile_has_wind(seeded_db: Path) -> None:
    """wind_speed_ms è presente in ogni slot del profilo."""
    _insert_hourly_nwp(seeded_db, "casa_campi", "2026-05-19")
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = compute_hourly_profile(db, "casa_campi", "2026-05-19", 5.0, 20.0, 3.0)
    assert result is not None
    assert all("wind_speed_ms" in r for r in result)


def test_hourly_profile_ci80_bands_temp(seeded_db: Path) -> None:
    """Bande CI 80% temperatura: temp_ci80_lo < temp_c < temp_ci80_hi ad ogni ora."""
    _insert_hourly_nwp(seeded_db, "casa_campi", "2026-05-19")
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        # p50 = 5..20, CI 80% bounds più larghi (3..22 per tmin, 4..23 per tmax)
        result = compute_hourly_profile(
            db, "casa_campi", "2026-05-19",
            tmin_p50=5.0, tmax_p50=20.0, precip_anchor=0.0,
            tmin_ci80_lo=3.0, tmin_ci80_hi=7.0,
            tmax_ci80_lo=18.0, tmax_ci80_hi=22.0,
        )
    assert result is not None
    for r in result:
        if r["temp_c"] is None:
            assert r["temp_ci80_lo"] is None
            assert r["temp_ci80_hi"] is None
        else:
            # p50 cade dentro la banda CI 80%
            assert r["temp_ci80_lo"] <= r["temp_c"] <= r["temp_ci80_hi"], (
                f"hour={r['hour']} p50={r['temp_c']} band=[{r['temp_ci80_lo']}, {r['temp_ci80_hi']}]"
            )
            # La banda ha larghezza > 0 (i bound sono distinti)
            assert r["temp_ci80_hi"] >= r["temp_ci80_lo"]


def test_hourly_profile_ci80_bands_precip(seeded_db: Path) -> None:
    """Bande CI 80% precipitazione: precip_ci80_lo ≤ precip_mm ≤ precip_ci80_hi."""
    _insert_hourly_nwp(seeded_db, "casa_campi", "2026-05-19")
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = compute_hourly_profile(
            db, "casa_campi", "2026-05-19",
            tmin_p50=5.0, tmax_p50=20.0, precip_anchor=3.0,
            precip_ci80_lo=1.0, precip_ci80_hi=6.0,
        )
    assert result is not None
    for r in result:
        if r["precip_mm"] is None:
            assert r["precip_ci80_lo"] is None
            assert r["precip_ci80_hi"] is None
        else:
            # p50 cade dentro la banda (per ogni ora)
            assert r["precip_ci80_lo"] <= r["precip_mm"] <= r["precip_ci80_hi"], (
                f"hour={r['hour']} p50={r['precip_mm']} band=[{r['precip_ci80_lo']}, {r['precip_ci80_hi']}]"
            )
            # Le bande non sono negative
            assert r["precip_ci80_lo"] >= 0
            assert r["precip_ci80_hi"] >= 0


def test_hourly_profile_ci80_none_is_noop(seeded_db: Path) -> None:
    """Se i bound CI 80% sono None, le bande orarie sono None (no crash)."""
    _insert_hourly_nwp(seeded_db, "casa_campi", "2026-05-19")
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = compute_hourly_profile(
            db, "casa_campi", "2026-05-19", 5.0, 20.0, 3.0,
            # tmin_ci80_lo/hi, tmax_ci80_lo/hi, precip_ci80_lo/hi tutti None
        )
    assert result is not None
    for r in result:
        if r["temp_c"] is not None:
            assert r["temp_ci80_lo"] is None
            assert r["temp_ci80_hi"] is None
        if r["precip_mm"] is not None:
            assert r["precip_ci80_lo"] is None
            assert r["precip_ci80_hi"] is None


# ── get_current_conditions ────────────────────────────────────────────────────

def test_current_conditions_no_data(seeded_db: Path) -> None:
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")
    assert result is None


def _seed_sir_weight(
    con: object, station_id: str, location_id: str, weight: float = 1.0
) -> None:
    """Riga station_weights per una stazione SIR (mapping stazione→location)."""
    con.execute(  # type: ignore[attr-defined]
        "INSERT INTO station_weights (station_id, source, location_id, weight) "
        "VALUES (?, 'sir', ?, ?)",
        [station_id, location_id, weight],
    )


def test_current_conditions_returns_data(seeded_db: Path) -> None:
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    ts_recent = now - timedelta(minutes=10)

    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, humidity_pct, precip_mm, wind_speed_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ["sir_toscana", "ST001", "casa_campi", ts_recent, "realtime", 18.5, 65.0, 0.0, 1.2])
    _seed_sir_weight(con, "ST001", "casa_campi")
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    assert "ts" in result
    # ts_sir popolato (dato da stazione SIR), ts_netatmo assente
    assert result["ts_sir"] is not None
    assert result["ts_netatmo"] is None
    assert result["temp_c"] == pytest.approx(18.5)
    assert result["humidity_pct"] == pytest.approx(65.0)
    assert result["precip_mm"] == pytest.approx(0.0)
    assert result["wind_speed_ms"] == pytest.approx(1.2)
    # Vento dal blend stazioni → provenance realtime
    assert result["wind_speed_source"] == "realtime"
    # sources per-variabile: tutte le variabili obs dal blend, pressure/weather null
    sources = result["sources"]
    assert sources["temp_c"] == "realtime"
    assert sources["humidity_pct"] == "realtime"
    assert sources["precip_mm"] == "realtime"
    assert sources["wind_speed_ms"] == "realtime"
    assert sources["wind_dir_deg"] is None
    assert sources["pressure_hpa"] is None
    assert sources["weather_code"] is None
    assert result["wind_speed_source"] == sources["wind_speed_ms"]  # alias coerente


def test_current_conditions_wind_fallback_per_variable(seeded_db: Path) -> None:
    """P3: temp realtime SIR senza vento → wind_speed_ms ripiegato sul NWP (nwp).

    La riga forecast ha SOLO il vento (temp_c null): senza il filtro
    temp_c IS NOT NULL nel fallback non sarebbe selezionata.
    """
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, humidity_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ["sir_toscana", "ST_W", "casa_campi", now - timedelta(minutes=5), "realtime", 22.0, 55.0])
    _seed_sir_weight(con, "ST_W", "casa_campi")
    con.execute("""
        INSERT INTO forecasts
            (source, location_id, ts_run, ts_valid, lead_time_h, wind_speed_ms)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ["open_meteo_ecmwf_ifs", "casa_campi",
          now - timedelta(hours=2), now - timedelta(minutes=10), 2, 4.5])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    # temp dal blend stazioni (obs realtime), con ts_sir valorizzato
    assert result["temp_c"] == pytest.approx(22.0)
    assert result["ts_sir"] is not None
    assert result["ts_netatmo"] is None
    # vento dal fallback NWP, provenance esplicita
    assert result["wind_speed_ms"] == pytest.approx(4.5)
    assert result["wind_speed_source"] == "nwp"
    # sources coerente: temp realtime, vento NWP, variabili mancanti null
    sources = result["sources"]
    assert sources["temp_c"] == "realtime"
    assert sources["humidity_pct"] == "realtime"
    assert sources["precip_mm"] is None
    assert sources["wind_speed_ms"] == "nwp"
    assert sources["wind_dir_deg"] is None
    assert sources["pressure_hpa"] is None
    assert sources["weather_code"] is None
    assert result["wind_speed_source"] == sources["wind_speed_ms"]  # alias coerente


def _seed_arpat_weight(
    con: object, station_id: str, location_id: str, weight: float = 1.0
) -> None:
    """Riga station_weights per una stazione ARPAT (mapping stazione→location)."""
    con.execute(  # type: ignore[attr-defined]
        "INSERT INTO station_weights (station_id, source, location_id, weight) "
        "VALUES (?, 'arpat', ?, ?)",
        [station_id, location_id, weight],
    )


def test_air_quality_resolved_via_station_weights(seeded_db: Path) -> None:
    """L'AQ si risolve via station_weights, non via obs.location_id: una stazione
    condivisa, taggata in obs ad un'altra location, contribuisce comunque alla
    location target (scenario casa_cercina/casa_nicco con stazioni ARPAT condivise)."""
    from datetime import timedelta

    import duckdb

    ts_recent = datetime.now() - timedelta(hours=1)
    con = duckdb.connect(str(seeded_db))
    # obs taggata 'casa_nicco' (ha vinto la PK), ma il peso la mappa a 'casa_cercina'
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, no2_ugm3, o3_ugm3)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ["arpat", "FI-MOSSE", "casa_nicco", ts_recent, "hourly", 40.0, 60.0])
    _seed_arpat_weight(con, "FI-MOSSE", "casa_cercina")
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_air_quality(db, "casa_cercina")

    assert result is not None
    assert result["no2_ugm3"] == pytest.approx(40.0)
    assert result["o3_ugm3"] == pytest.approx(60.0)


def test_air_quality_weighted_average(seeded_db: Path) -> None:
    """Media pesata per stazione: due stazioni con pesi diversi sullo stesso inquinante."""
    from datetime import timedelta

    import duckdb

    ts = datetime.now() - timedelta(hours=1)
    con = duckdb.connect(str(seeded_db))
    con.executemany("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, no2_ugm3)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [
        ["arpat", "FI-MOSSE",     "x", ts, "hourly", 30.0],
        ["arpat", "FI-LAVAGNINI", "x", ts, "hourly", 50.0],
    ])
    _seed_arpat_weight(con, "FI-MOSSE", "casa_cercina", 0.75)
    _seed_arpat_weight(con, "FI-LAVAGNINI", "casa_cercina", 0.25)
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_air_quality(db, "casa_cercina")

    assert result is not None
    # 30*0.75 + 50*0.25 = 35.0
    assert result["no2_ugm3"] == pytest.approx(35.0)


def test_air_quality_none_without_weights(seeded_db: Path) -> None:
    """Senza station_weights ARPAT (weights refresh non eseguito) l'AQ è None."""
    from datetime import timedelta

    import duckdb

    ts = datetime.now() - timedelta(hours=1)
    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, no2_ugm3)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ["arpat", "FI-MOSSE", "casa_cercina", ts, "hourly", 40.0])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_air_quality(db, "casa_cercina")

    assert result is None


def test_current_conditions_old_data_ignored(seeded_db: Path) -> None:
    """Obs più vecchie di 3h non devono contribuire al risultato."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    ts_old = now - timedelta(hours=4)

    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ["sir_toscana", "ST001", "casa_campi", ts_old, "realtime", 10.0])
    _seed_sir_weight(con, "ST001", "casa_campi")
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")
    assert result is None


def test_current_conditions_shared_station_wind(seeded_db: Path) -> None:
    """Stazione condivisa taggata con un'altra location: il vento deve comunque
    comparire per la location target via station_weights (bug casa_nicco)."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    # La riga obs è taggata 'lavoro_cosimo' (ha vinto la corsa in ingest),
    # ma la stazione è pesata anche da 'casa_nicco'.
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, wind_speed_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ["sir_toscana", "ST_SHARED", "lavoro_cosimo", now - timedelta(minutes=5), "realtime", 17.0, 3.4])
    _seed_sir_weight(con, "ST_SHARED", "lavoro_cosimo")
    _seed_sir_weight(con, "ST_SHARED", "casa_nicco")
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_nicco")

    assert result is not None
    assert result["wind_speed_ms"] == pytest.approx(3.4)
    # Vento dal blend stazioni (via station_weights) → provenance realtime
    assert result["wind_speed_source"] == "realtime"
    assert result["sources"]["wind_speed_ms"] == "realtime"
    assert result["sources"]["temp_c"] == "realtime"


def test_current_conditions_weighted_blend(seeded_db: Path) -> None:
    """Due stazioni con pesi diversi: media pesata, non media semplice."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.executemany("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [
        ["sir_toscana", "ST_A", "casa_campi", now - timedelta(minutes=5), "realtime", 10.0],
        ["sir_toscana", "ST_B", "casa_campi", now - timedelta(minutes=5), "realtime", 20.0],
    ])
    _seed_sir_weight(con, "ST_A", "casa_campi", weight=3.0)
    _seed_sir_weight(con, "ST_B", "casa_campi", weight=1.0)
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    # (10*3 + 20*1) / (3+1) = 12.5, non la media semplice 15.0
    assert result["temp_c"] == pytest.approx(12.5)


def test_current_conditions_netatmo_blend(seeded_db: Path) -> None:
    """Netatmo contribuisce via observations.weight (non è in station_weights)."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, weight, qc_pass)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, ["netatmo", "70:ee:50:aa", "casa_campi", now - timedelta(minutes=5), "realtime", 19.0, 0.4, True])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    assert result["temp_c"] == pytest.approx(19.0)
    # ts_netatmo popolato, ts_sir assente (nessuna stazione SIR)
    assert result["ts_netatmo"] is not None
    assert result["ts_sir"] is None
    # Nessun anemometro tra le obs Netatmo e nessun forecast NWP → vento assente
    assert result["wind_speed_source"] is None
    # sources: temp realtime, vento assente, pressure/weather assenti
    sources = result["sources"]
    assert sources["temp_c"] == "realtime"
    assert sources["wind_speed_ms"] is None
    assert sources["pressure_hpa"] is None
    assert sources["weather_code"] is None


def test_current_conditions_excludes_netatmo_qc_fail(seeded_db: Path) -> None:
    """Un modulo Netatmo con qc_pass=False non entra nella media realtime."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.executemany("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, weight, qc_pass)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        ["netatmo", "70:ee:50:aa", "casa_campi", now - timedelta(minutes=5), "realtime", 19.0, 0.4, True],
        # Valore sballato (irraggiamento solare): scartato dal QC, deve essere ignorato
        ["netatmo", "70:ee:50:bb", "casa_campi", now - timedelta(minutes=5), "realtime", 45.0, 0.4, False],
    ])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    # Solo il modulo qc_pass=True contribuisce: 19.0, non la media 32.0
    assert result["temp_c"] == pytest.approx(19.0)


def test_current_conditions_netatmo_sublinear_weight(seeded_db: Path) -> None:
    """Il peso aggregato Netatmo cresce come sqrt(N), non come N: la densità di
    moduli non sommerge le stazioni SIR validate."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    # 1 SIR a 10°C (peso 1.0) + 4 moduli Netatmo a 20°C (peso 1.0 ciascuno).
    con.execute("""
        INSERT INTO observations (source, station_id, location_id, ts, granularity, temp_c)
        VALUES ('sir_toscana', 'ST_A', 'casa_campi', ?, 'realtime', 10.0)
    """, [now - timedelta(minutes=5)])
    _seed_sir_weight(con, "ST_A", "casa_campi", weight=1.0)
    con.executemany("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, weight, qc_pass)
        VALUES ('netatmo', ?, 'casa_campi', ?, 'realtime', 20.0, 1.0, TRUE)
    """, [[f"70:ee:50:{i:02x}", now - timedelta(minutes=5)] for i in range(4)])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    # Senza scalatura: (10 + 20*4)/(1+4) = 18.0.
    # Con 1/sqrt(4)=0.5: (10 + 20*0.5*4)/(1 + 0.5*4) = 50/3 = 16.67.
    assert result["temp_c"] == pytest.approx(50.0 / 3.0, abs=0.05)


def test_dewpoint_known_value() -> None:
    """T=20°C, RH=50% → Td ≈ 9.3°C (valore di riferimento Magnus)."""
    assert _dewpoint(20.0, 50.0) == pytest.approx(9.3, abs=0.2)


def test_current_conditions_has_derived_fields(seeded_db: Path) -> None:
    """get_current_conditions aggiunge dewpoint_c e feels_like_c."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, humidity_pct, precip_mm, wind_speed_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, ["sir_toscana", "ST_DEW", "casa_campi", now - timedelta(minutes=5), "realtime", 20.0, 50.0, 0.0, 2.0])
    _seed_sir_weight(con, "ST_DEW", "casa_campi")
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    assert "dewpoint_c" in result
    assert "feels_like_c" in result
    assert result["dewpoint_c"] is not None
    assert result["dewpoint_c"] < result["temp_c"]  # rugiada sempre < temperatura


def test_current_conditions_pressure_null_without_forecasts(seeded_db: Path) -> None:
    """pressure_hpa è None se non ci sono forecast NWP recenti."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, humidity_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ["sir_toscana", "ST001", "casa_campi", now - timedelta(minutes=10), "realtime", 18.0, 60.0])
    _seed_sir_weight(con, "ST001", "casa_campi")
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    assert result["pressure_hpa"] is None


def test_current_conditions_pressure_from_forecasts(seeded_db: Path) -> None:
    """pressure_hpa viene dalla tabella forecasts (Open-Meteo), non da observations."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, humidity_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ["sir_toscana", "ST001", "casa_campi", now - timedelta(minutes=10), "realtime", 18.0, 60.0])
    _seed_sir_weight(con, "ST001", "casa_campi")
    con.execute("""
        INSERT INTO forecasts
            (source, location_id, ts_run, ts_valid, lead_time_h, pressure_hpa)
        VALUES (?, ?, ?, ?, ?, ?)
    """, ["open_meteo_icon_eu", "casa_campi", now - timedelta(hours=2), now - timedelta(minutes=30), 2, 1018.5])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    assert result["pressure_hpa"] == pytest.approx(1018.5, abs=0.5)
    # pressure è sempre NWP quando valorizzata
    assert result["sources"]["pressure_hpa"] == "nwp"
    # temp dal blend stazioni (obs realtime)
    assert result["sources"]["temp_c"] == "realtime"


def test_current_conditions_fallback_nwp(seeded_db: Path) -> None:
    """Nessuna osservazione: current viene dal NWP (media tra modelli, ora vicina)."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.executemany("""
        INSERT INTO forecasts
            (source, location_id, ts_run, ts_valid, lead_time_h, temp_c, wind_speed_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [
        ["open_meteo_arome_france", "casa_campi", now - timedelta(hours=2), now - timedelta(minutes=20), 2, 16.0, 2.0],
        ["open_meteo_ecmwf_ifs", "casa_campi", now - timedelta(hours=2), now - timedelta(minutes=20), 2, 18.0, 4.0],
    ])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    assert result["temp_c"] == pytest.approx(17.0)  # media (16+18)/2
    assert result["wind_speed_ms"] == pytest.approx(3.0)
    # Nessuna obs: tutte le variabili (vento incluso) dal NWP
    assert result["wind_speed_source"] == "nwp"
    # sources coerente: ogni variabile valorizzata è "nwp" nel ramo tutto-NWP
    for key in ("temp_c", "humidity_pct", "precip_mm", "wind_speed_ms", "wind_dir_deg"):
        assert result["sources"][key] in ("nwp", None), key
    assert result["sources"]["wind_speed_ms"] == "nwp"
    assert result["sources"]["temp_c"] == "nwp"


# ── get_nwp_models_hourly ─────────────────────────────────────────────────────

def test_nwp_models_hourly_empty(seeded_db: Path) -> None:
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_nwp_models_hourly(db, "casa_campi")
    assert result == []


def test_nwp_models_hourly_structure_and_order(seeded_db: Path) -> None:
    """Due modelli futuri: verifica struttura, ordine _MODEL_ORDER, e campi ts."""
    from datetime import timedelta

    import duckdb

    ts_run = datetime.now()
    records = []
    for src in ("open_meteo_icon_eu", "open_meteo_ecmwf_ifs"):
        for h in range(3):
            ts_valid = datetime.now() + timedelta(hours=h + 1)
            lead = h + 1
            records.append((src, "casa_campi", ts_run, ts_valid, lead, 14.0 + h, 75.0, 0.0))

    con = duckdb.connect(str(seeded_db))
    con.executemany(
        "INSERT OR REPLACE INTO forecasts "
        "(source, location_id, ts_run, ts_valid, lead_time_h, temp_c, humidity_pct, precip_mm) "
        "VALUES (?,?,?,?,?,?,?,?)",
        records,
    )
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_nwp_models_hourly(db, "casa_campi")

    assert len(result) == 2
    # ECMWF IFS viene prima di ICON-EU in _MODEL_ORDER
    assert result[0]["source"] == "open_meteo_ecmwf_ifs"
    assert result[1]["source"] == "open_meteo_icon_eu"
    assert result[0]["label"] == "ECMWF IFS"

    first_entry = result[0]["data"][0]
    assert "ts" in first_entry
    assert first_entry["ts"].endswith("Z"), "ts deve terminare con Z (UTC)"
    assert "temp_c" in first_entry
    assert "humidity_pct" in first_entry
    assert "precip_mm" in first_entry
    assert "wind_speed_ms" in first_entry


# ── write_location_json — with db ─────────────────────────────────────────────

def test_write_location_json_with_db_adds_realtime_fields(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
    seeded_db: Path,
) -> None:
    """Passando db, il JSON deve includere current, air_quality, nwp_models_hourly."""
    days = _make_days(sample_pred, sample_indicators)
    with DuckDBClient(db_path=seeded_db) as db:
        path = write_location_json(
            location_id="casa_campi",
            days=days,
            coverage={},
            output_dir=tmp_path,
            db=db,
        )
    data = json.loads(path.read_text())
    assert "current" in data
    assert "air_quality" in data
    assert "nwp_models_hourly" in data
    # Con DB vuoto, current e air_quality saranno None, nwp_models_hourly []
    assert data["nwp_models_hourly"] == []


# ── _modal_weather_code ───────────────────────────────────────────────────────

def test_modal_weather_code_single() -> None:
    assert _modal_weather_code([3]) == 3


def test_modal_weather_code_clear_winner() -> None:
    assert _modal_weather_code([0, 3, 3, 61]) == 3


def test_modal_weather_code_tiebreak_severity() -> None:
    # 0 e 61 in parità: 61 ha severità più alta
    assert _modal_weather_code([0, 61]) == 61


def test_modal_weather_code_empty() -> None:
    assert _modal_weather_code([]) is None


def test_modal_weather_code_unknown_code_gets_zero_severity() -> None:
    # Codice sconosciuto 999 vs 0: parità → 999 vince per max() con stessa priorità 0,
    # ma l'importante è che non sollevi eccezioni.
    result = _modal_weather_code([0, 999])
    assert result in (0, 999)


def test_wmo_severity_complete_for_common_codes() -> None:
    """I codici WMO più comuni devono avere una severità definita."""
    for code in [0, 1, 2, 3, 45, 48, 51, 61, 63, 65, 71, 75, 80, 81, 95, 96, 99]:
        assert code in _WMO_SEVERITY, f"Codice {code} mancante in _WMO_SEVERITY"


# ── get_daily_weather_code ────────────────────────────────────────────────────

def _insert_forecasts_with_wc(db_path: Path, location_id: str, target_date: str) -> None:
    """Inserisce forecast NWP con weather_code per due modelli su 20 ore ciascuno.

    Distribuzione: 10 ore code 3 (coperto) + 10 ore code 61 (pioggia) per source,
    così entrambi i code sono in parità (20 vs 20) → tie-break a favore di 61.
    20 ore soddisfano la soglia _MIN_HOURS_FOR_DAILY_CODE.
    """
    from datetime import date

    import duckdb

    d = date.fromisoformat(target_date)
    ts_run = datetime(d.year, d.month, d.day, 0, 0, 0)
    records = []
    for src in ("open_meteo_ecmwf_ifs", "open_meteo_icon_eu"):
        for h in range(20):
            wc = 3 if h < 10 else 61
            ts_valid = datetime(d.year, d.month, d.day, h, 0, 0)
            records.append((src, location_id, ts_run, ts_valid, h, 18.0, wc))

    con = duckdb.connect(str(db_path))
    con.executemany(
        "INSERT OR REPLACE INTO forecasts "
        "(source, location_id, ts_run, ts_valid, lead_time_h, temp_c, weather_code) "
        "VALUES (?,?,?,?,?,?,?)",
        records,
    )
    con.close()


def test_get_daily_weather_code_pessimistic(seeded_db: Path) -> None:
    """Caso pessimistico: 61 appare 10 ore su 20 → vince su 3 (10 ore) per severità."""
    _insert_forecasts_with_wc(seeded_db, "casa_campi", "2026-05-19")
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_daily_weather_code(db, "casa_campi", "2026-05-19")
    # 10 ore code 3, 10 ore code 61 — entrambi stabili (≥2h); 61 ha severità maggiore
    assert result == 61


def test_get_daily_weather_code_empty(seeded_db: Path) -> None:
    """Nessun dato → None."""
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_daily_weather_code(db, "casa_campi", "2026-05-19")
    assert result is None


def test_get_daily_weather_code_latest_run_wins(seeded_db: Path) -> None:
    """QUALIFY latest run: il run più recente prevale per ogni (source, ts_valid).

    Inserisce 20 ore per open_meteo_icon_eu (sopra soglia): per l'ora 12 ci sono due run,
    il vecchio con wc=0 e il nuovo con wc=61. Tutte le altre ore hanno wc=61.
    Il risultato atteso è 61 (il run più recente dell'ora 12 sovrascrive il vecchio).
    """
    import duckdb

    d_str = "2026-05-19"
    con = duckdb.connect(str(seeded_db))
    # 20 ore con run-base wc=61, poi ora 12 ha anche un run vecchio con wc=0
    records = [
        ("open_meteo_icon_eu", "casa_campi", f"{d_str} 06:00", f"{d_str} {h:02d}:00", h, 18.0, 61)
        for h in range(20)
    ] + [
        # run vecchio per ora 12 con codice 0 — deve essere ignorato
        ("open_meteo_icon_eu", "casa_campi", f"{d_str} 00:00", f"{d_str} 12:00", 12, 18.0, 0),
    ]
    con.executemany(
        "INSERT INTO forecasts (source, location_id, ts_run, ts_valid, lead_time_h, temp_c, weather_code)"
        " VALUES (?,?,?,?,?,?,?)",
        records,
    )
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_daily_weather_code(db, "casa_campi", "2026-05-19")
    # run 06:00 (wc=61) batte run 00:00 (wc=0) per l'ora 12 → moda è 61
    assert result == 61


def test_get_daily_weather_code_spike_filtered(seeded_db: Path) -> None:
    """Spike da 1 ora (codice 95) viene ignorato; vince il codice stabile più severo."""
    import duckdb

    d_str = "2026-05-19"
    con = duckdb.connect(str(seeded_db))
    records = [
        # 19 ore code 3 (coperto)
        ("open_meteo_ecmwf_ifs", "casa_campi", f"{d_str} 00:00", f"{d_str} {h:02d}:00", h, 18.0, 3)
        for h in range(19)
    ] + [
        # 1 ora code 95 (temporale) — spike singolo
        ("open_meteo_ecmwf_ifs", "casa_campi", f"{d_str} 00:00", f"{d_str} 19:00", 19, 18.0, 95),
    ]
    con.executemany(
        "INSERT INTO forecasts (source, location_id, ts_run, ts_valid, lead_time_h, temp_c, weather_code)"
        " VALUES (?,?,?,?,?,?,?)",
        records,
    )
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_daily_weather_code(db, "casa_campi", "2026-05-19")
    # 95 appare solo 1 ora (< soglia 2) → fallback a codici stabili → vince 3
    assert result == 3


def test_get_daily_weather_code_excludes_partial_sources(seeded_db: Path) -> None:
    """Source con meno di _MIN_HOURS_FOR_DAILY_CODE ore è esclusa dalla moda."""
    import duckdb

    d_str = "2026-05-19"
    con = duckdb.connect(str(seeded_db))
    # Source completa (20 ore): codice 3 (coperto) per tutte
    records_full = [
        ("open_meteo_ecmwf_ifs", "casa_campi", f"{d_str} 00:00", f"{d_str} {h:02d}:00", h, 18.0, 3)
        for h in range(20)
    ]
    # Source parziale (5 ore): codice 95 (temporale) — non deve influenzare la moda
    records_partial = [
        ("open_meteo_icon_eu", "casa_campi", f"{d_str} 00:00", f"{d_str} {h:02d}:00", h, 18.0, 95)
        for h in range(5)
    ]
    con.executemany(
        "INSERT INTO forecasts (source, location_id, ts_run, ts_valid, lead_time_h, temp_c, weather_code)"
        " VALUES (?,?,?,?,?,?,?)",
        records_full + records_partial,
    )
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_daily_weather_code(db, "casa_campi", "2026-05-19")
    # icon_eu (5 ore) è escluso → solo ecmwf_ifs contribuisce → moda è 3
    assert result == 3


# ── current.weather_code ─────────────────────────────────────────────────────

def test_current_conditions_has_weather_code_key(seeded_db: Path) -> None:
    """get_current_conditions deve sempre avere la chiave weather_code nel risultato."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, humidity_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ["sir_toscana", "ST001", "casa_campi", now - timedelta(minutes=5), "realtime", 18.0, 60.0])
    _seed_sir_weight(con, "ST001", "casa_campi")
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    assert "weather_code" in result
    # Senza forecast NWP recenti, deve essere None
    assert result["weather_code"] is None


def test_current_conditions_weather_code_from_forecasts(seeded_db: Path) -> None:
    """weather_code viene dai forecast NWP recenti."""
    from datetime import timedelta

    import duckdb

    now = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, humidity_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ["sir_toscana", "ST001", "casa_campi", now - timedelta(minutes=5), "realtime", 18.0, 60.0])
    _seed_sir_weight(con, "ST001", "casa_campi")
    # Forecast nell'ora corrente con weather_code = 3 (coperto)
    con.execute("""
        INSERT INTO forecasts
            (source, location_id, ts_run, ts_valid, lead_time_h, temp_c, weather_code)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ["open_meteo_icon_eu", "casa_campi",
          now - timedelta(hours=1), now - timedelta(minutes=10), 1, 18.5, 3])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    assert result["weather_code"] == 3
    assert isinstance(result["weather_code"], int)
    # weather_code è sempre NWP quando valorizzato
    assert result["sources"]["weather_code"] == "nwp"


# ── write_location_json — weather_code propagation ────────────────────────────

def test_write_location_json_weather_code_propagated(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
) -> None:
    """weather_code nel day_entry viene propagato nel JSON di output."""
    days = [
        {"target_date": "2026-05-19", "lead_time_h": 24,
         "pred": sample_pred, "indicators": sample_indicators,
         "weather_code": 61},
    ]
    path = write_location_json(
        location_id="casa_campi", days=days, coverage={}, output_dir=tmp_path,
    )
    data = json.loads(path.read_text())
    assert data["days"][0]["weather_code"] == 61
    assert isinstance(data["days"][0]["weather_code"], int)


def test_write_location_json_weather_code_null(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
) -> None:
    """weather_code None è serializzato come null nel JSON."""
    days = [
        {"target_date": "2026-05-19", "lead_time_h": 24,
         "pred": sample_pred, "indicators": sample_indicators},
        # weather_code assente → None
    ]
    path = write_location_json(
        location_id="casa_campi", days=days, coverage={}, output_dir=tmp_path,
    )
    data = json.loads(path.read_text())
    assert data["days"][0]["weather_code"] is None


# ── refresh_realtime_json ─────────────────────────────────────────────────────

def _seed_realtime_sir(db_path: Path, location_id: str, temp_c: float) -> None:
    """Inserisce una osservazione SIR realtime recente con station_weights."""
    from datetime import timedelta

    import duckdb

    con = duckdb.connect(str(db_path))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, temp_c, humidity_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ["sir_toscana", "ST_RT", location_id,
          datetime.now() - timedelta(minutes=5), "realtime", temp_c, 60.0])
    _seed_sir_weight(con, "ST_RT", location_id)
    con.close()


def test_refresh_realtime_json_updates_current(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
    seeded_db: Path,
) -> None:
    """refresh_realtime_json sostituisce current con le osservazioni appena ingerite."""
    with DuckDBClient(db_path=seeded_db) as db:
        path = write_location_json(
            location_id="casa_campi",
            days=_make_days(sample_pred, sample_indicators),
            coverage={},
            output_dir=tmp_path,
            db=db,
        )
        # DB vuoto → la pipeline ha scritto current null
        assert json.loads(path.read_text())["current"] is None

        _seed_realtime_sir(seeded_db, "casa_campi", 21.5)
        updated = refresh_realtime_json(db, "casa_campi", tmp_path)

    assert updated == path
    data = json.loads(path.read_text())
    assert data["current"] is not None
    assert data["current"]["temp_c"] == pytest.approx(21.5)
    assert data["current"]["humidity_pct"] == pytest.approx(60.0)
    # air_quality ricalcolato (None senza dati ARPAT), mai rimosso dal payload
    assert "air_quality" in data


def test_refresh_realtime_json_preserves_forecast_fields(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
    seeded_db: Path,
) -> None:
    """I campi non realtime restano intatti: solo current/air_quality cambiano."""
    from datetime import timedelta

    import duckdb

    # Forecast NWP orari: nwp_models_hourly non deve essere vuoto né cambiare
    ts_run = datetime.now()
    con = duckdb.connect(str(seeded_db))
    con.executemany("""
        INSERT OR REPLACE INTO forecasts
        (source, location_id, ts_run, ts_valid, lead_time_h, temp_c, humidity_pct, precip_mm)
        VALUES (?,?,?,?,?,?,?,?)
    """, [
        ["open_meteo_ecmwf_ifs", "casa_campi", ts_run, datetime.now() + timedelta(hours=1), 1, 20.0, 60.0, 0.0],
        ["open_meteo_icon_eu",   "casa_campi", ts_run, datetime.now() + timedelta(hours=2), 2, 21.0, 55.0, 0.0],
    ])
    con.close()

    coverage = {"tmin_ci80": 0.80, "tmin_ci90": 0.91, "tmax_ci80": None,
                "tmax_ci90": None, "precip_ci80": None, "precip_ci90": None}
    days = _make_days(sample_pred, sample_indicators,
                      dates=[("2026-05-19", 24), ("2026-05-20", 48)])

    with DuckDBClient(db_path=seeded_db) as db:
        path = write_location_json(
            location_id="casa_campi", days=days, coverage=coverage,
            output_dir=tmp_path, db=db,
        )
        before = json.loads(path.read_text())
        assert before["current"] is not None  # fallback NWP (nessuna obs)

        _seed_realtime_sir(seeded_db, "casa_campi", 18.0)
        refresh_realtime_json(db, "casa_campi", tmp_path)

    after = json.loads(path.read_text())

    # Campi non realtime preservati integralmente
    assert after["location_id"] == before["location_id"]
    assert after["generated_at"] == before["generated_at"]
    assert after["coverage_empirical_30d"] == before["coverage_empirical_30d"]
    assert after["nwp_models_hourly"] == before["nwp_models_hourly"]
    assert after["days"] == before["days"]
    # Solo current è cambiato (obs realtime batte il fallback NWP)
    assert after["current"] != before["current"]
    assert after["current"]["temp_c"] == pytest.approx(18.0)


def test_refresh_realtime_json_missing_file_skips(
    seeded_db: Path,
    tmp_path: Path,
) -> None:
    """JSON non ancora generato → skip: nessun file creato, nessun temp residuo."""
    with DuckDBClient(db_path=seeded_db) as db:
        result = refresh_realtime_json(db, "casa_campi", tmp_path)

    assert result is None
    assert not (tmp_path / "casa_campi.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_refresh_realtime_json_updates_air_quality_and_clean_tmp(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
    seeded_db: Path,
) -> None:
    """air_quality aggiornato con dati ARPAT recenti; JSON valido e atomico."""
    from datetime import timedelta

    import duckdb

    ts = datetime.now() - timedelta(hours=1)
    con = duckdb.connect(str(seeded_db))
    con.execute("""
        INSERT INTO observations
            (source, station_id, location_id, ts, granularity, no2_ugm3, o3_ugm3)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, ["arpat", "FI-MOSSE", "casa_campi", ts, "hourly", 42.0, 58.0])
    _seed_arpat_weight(con, "FI-MOSSE", "casa_campi")
    con.close()

    with DuckDBClient(db_path=seeded_db) as db:
        path = write_location_json(
            location_id="casa_campi",
            days=_make_days(sample_pred, sample_indicators),
            coverage={},
            output_dir=tmp_path,
        )
        # JSON senza db: air_quality assente → il refresh lo aggiunge
        assert "air_quality" not in json.loads(path.read_text())

        refresh_realtime_json(db, "casa_campi", tmp_path)

    data = json.loads(path.read_text())
    assert data["air_quality"] == {
        "pm10_ugm3": None, "pm25_ugm3": None, "no2_ugm3": 42.0, "o3_ugm3": 58.0,
        "co_mgm3": None, "benzene_ugm3": None, "so2_ugm3": None,
    }
    # Scrittura atomica: nessun file temporaneo residuo dopo il replace
    assert list(tmp_path.glob("*.tmp")) == []
    # Il JSON resta leggibile e i forecast invariati
    assert data["days"][0]["forecasts"]["tmin_c"]["p50"] == sample_pred["tmin_c"]["p50"]


# ── updates metadata temporale ────────────────────────────────────────────────

def test_write_location_json_preserves_realtime_at(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
) -> None:
    """Una riscrittura pipeline conserva updates.realtime_at del payload precedente."""
    write_location_json(
        location_id="casa_campi",
        days=_make_days(sample_pred, sample_indicators),
        coverage={},
        output_dir=tmp_path,
    )
    # Simula un refresh realtime intervenuto tra una pipeline e l'altra
    path = tmp_path / "casa_campi.json"
    payload = json.loads(path.read_text())
    payload["updates"]["realtime_at"] = "2026-05-18T10:00:00+00:00"
    path.write_text(json.dumps(payload))

    write_location_json(
        location_id="casa_campi",
        days=_make_days(sample_pred, sample_indicators),
        coverage={},
        output_dir=tmp_path,
    )
    data = json.loads(path.read_text())
    assert data["updates"]["realtime_at"] == "2026-05-18T10:00:00+00:00"
    assert data["updates"]["pipeline_at"] == data["generated_at"]


def test_refresh_realtime_json_sets_realtime_at(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
    seeded_db: Path,
) -> None:
    """Il refresh imposta updates.realtime_at preservando pipeline_at e generated_at."""
    with DuckDBClient(db_path=seeded_db) as db:
        path = write_location_json(
            location_id="casa_campi",
            days=_make_days(sample_pred, sample_indicators),
            coverage={},
            output_dir=tmp_path,
        )
        before = json.loads(path.read_text())
        pipeline_at = before["updates"]["pipeline_at"]
        generated_at = before["generated_at"]

        _seed_realtime_sir(seeded_db, "casa_campi", 21.5)
        refresh_realtime_json(db, "casa_campi", tmp_path)

    data = json.loads(path.read_text())
    assert data["updates"]["pipeline_at"] == pipeline_at
    assert data["updates"]["realtime_at"] is not None
    # Il refresh non tocca generated_at (resta quello della pipeline)
    assert data["generated_at"] == generated_at
    assert data["updates"]["realtime_at"] != pipeline_at


def test_refresh_realtime_json_normalizes_legacy(
    tmp_path: Path,
    sample_pred: dict,
    sample_indicators: list[IndicatorResult],
    seeded_db: Path,
) -> None:
    """JSON legacy senza updates → struttura valida con pipeline_at null."""
    with DuckDBClient(db_path=seeded_db) as db:
        path = write_location_json(
            location_id="casa_campi",
            days=_make_days(sample_pred, sample_indicators),
            coverage={},
            output_dir=tmp_path,
        )
        # Rimuove updates: simula un JSON prodotto prima della metadata temporale
        payload = json.loads(path.read_text())
        del payload["updates"]
        path.write_text(json.dumps(payload))

        _seed_realtime_sir(seeded_db, "casa_campi", 21.5)
        refresh_realtime_json(db, "casa_campi", tmp_path)

    data = json.loads(path.read_text())
    assert data["updates"]["pipeline_at"] is None
    assert data["updates"]["realtime_at"] is not None
