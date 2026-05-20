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
    _dewpoint,
    _prob_exceeds,
    build_signals,
    build_signals_today,
    compute_coverage_30d,
    compute_hourly_profile,
    get_current_conditions,
    get_nwp_model_comparison,
    get_nwp_models_hourly,
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
    assert all(r["precip_mm"] == 0.0 for r in result)


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
    assert len(hum_values) == 24
    assert all(h >= 0 for h in hum_values)


def test_hourly_profile_has_wind(seeded_db: Path) -> None:
    """wind_speed_ms è presente in ogni slot del profilo."""
    _insert_hourly_nwp(seeded_db, "casa_campi", "2026-05-19")
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = compute_hourly_profile(db, "casa_campi", "2026-05-19", 5.0, 20.0, 3.0)
    assert result is not None
    assert all("wind_speed_ms" in r for r in result)


# ── get_current_conditions ────────────────────────────────────────────────────

def test_current_conditions_no_data(seeded_db: Path) -> None:
    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")
    assert result is None


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
    """, ["sir", "ST001", "casa_campi", ts_recent, "realtime", 18.5, 65.0, 0.0, 1.2])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    assert "ts" in result
    assert result["temp_c"] == pytest.approx(18.5)
    assert result["humidity_pct"] == pytest.approx(65.0)
    assert result["precip_mm"] == pytest.approx(0.0)
    assert result["wind_speed_ms"] == pytest.approx(1.2)


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
    """, ["sir", "ST001", "casa_campi", ts_old, "realtime", 10.0])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")
    assert result is None


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
    """, ["sir", "ST_DEW", "casa_campi", now - timedelta(minutes=5), "realtime", 20.0, 50.0, 0.0, 2.0])
    con.close()

    with DuckDBClient(db_path=seeded_db, read_only=True) as db:
        result = get_current_conditions(db, "casa_campi")

    assert result is not None
    assert "dewpoint_c" in result
    assert "feels_like_c" in result
    assert result["dewpoint_c"] is not None
    assert result["dewpoint_c"] < result["temp_c"]  # rugiada sempre < temperatura


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
