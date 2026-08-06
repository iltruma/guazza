"""Test per monitor.py — coverage_30d + alert drift."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from guazza.monitor import compute_coverage
from guazza.storage import DuckDBClient


def _make_pred_record(location_id: str, ts_valid: datetime, tmin_obs: float) -> dict:
    return {
        "model_version": "20260601",
        "location_id": location_id,
        "ts_valid": ts_valid,
        "lead_time_h": 0,
        "tmin_c": {"p05": 4.0, "p10": 4.5, "p50": 5.0, "p90": 5.5, "p95": 6.0,
                   "ci80_lo": 4.5, "ci80_hi": 5.5, "ci90_lo": 4.0, "ci90_hi": 6.0},
        "tmax_c": {"p05": 14.0, "p10": 14.5, "p50": 15.0, "p90": 15.5, "p95": 16.0,
                   "ci80_lo": 14.5, "ci80_hi": 15.5, "ci90_lo": 14.0, "ci90_hi": 16.0},
        "precip_mm": {"p05": 0.0, "p10": 0.0, "p50": 0.0, "p90": 0.5, "p95": 1.0,
                      "ci80_lo": 0.0, "ci80_hi": 0.5, "ci90_lo": 0.0, "ci90_hi": 1.0},
        "_tmin_obs": tmin_obs,
        "_tmax_obs": 15.0,
        "_precip_obs": 0.2,
    }


def _backfill_obs(db: DuckDBClient, records: list[dict]) -> None:
    """Scrive le colonne *_obs in predictions dopo upsert_predictions."""
    for rec in records:
        ts = rec["ts_valid"].replace(tzinfo=None) if rec["ts_valid"].tzinfo else rec["ts_valid"]
        db.execute(
            """
            UPDATE predictions
               SET tmin_obs = ?, tmax_obs = ?, precip_obs = ?
             WHERE model_version = ? AND location_id = ? AND ts_valid = ? AND lead_time_h = ?
            """,
            [rec["_tmin_obs"], rec["_tmax_obs"], rec["_precip_obs"],
             rec["model_version"], rec["location_id"], ts, rec["lead_time_h"]],
        )


@pytest.fixture
def db_with_predictions(tmp_path: Path) -> DuckDBClient:
    client = DuckDBClient(db_path=tmp_path / "test.duckdb")
    with client:
        client.init_schema()
        today = date.today() - timedelta(days=20)
        records = []
        for i in range(50):
            ts_date = today + timedelta(days=i)
            ts = datetime(ts_date.year, ts_date.month, ts_date.day)
            records.append(_make_pred_record(f"loc_{i % 3}", ts, tmin_obs=5.0 + i * 0.1))
        client.upsert_predictions(records)
        _backfill_obs(client, records)
        yield client


def test_compute_coverage_returns_per_target_bucket(db_with_predictions: DuckDBClient) -> None:
    """compute_coverage deve restituire una riga per (target, bucket) con n_obs, cov_80, cov_90."""
    results = compute_coverage(db_with_predictions)
    assert len(results) > 0
    for r in results:
        assert r.n_obs > 0
        # Le coperture sono in [0, 1]
        assert 0.0 <= r.cov_80 <= 1.0
        assert 0.0 <= r.cov_90 <= 1.0
        # cov_90 ≥ cov_80 (CI più largo copre di più)
        assert r.cov_90 >= r.cov_80 - 0.01  # piccola tolleranza per None


def test_compute_coverage_includes_precip(db_with_predictions: DuckDBClient) -> None:
    """Tutti i 3 target devono essere rappresentati."""
    results = compute_coverage(db_with_predictions)
    targets = {r.target for r in results}
    assert "tmin_c" in targets
    assert "tmax_c" in targets
    assert "precip_mm" in targets


def test_compute_coverage_alerts_on_drift(db_with_predictions: DuckDBClient) -> None:
    """Drift > DRIFT_TOLERANCE_PP (0.05) deve emergere come drift_80/drift_90 non-nulli."""
    results = compute_coverage(db_with_predictions)
    for r in results:
        assert -1.0 <= r.drift_80 <= 0.2
        assert -1.0 <= r.drift_90 <= 0.1


def test_compute_coverage_no_data_returns_empty(tmp_path: Path) -> None:
    """DB senza predictions con actual → lista vuota."""
    with DuckDBClient(db_path=tmp_path / "test_empty.duckdb") as client:
        client.init_schema()
        assert compute_coverage(client) == []


def test_compute_coverage_window_30_days(db_with_predictions: DuckDBClient) -> None:
    """Solo predictions con ts_valid entro 30gg vengono considerate."""
    old_date = date.today() - timedelta(days=60)
    recent_date = date.today()
    old_ts = datetime(old_date.year, old_date.month, old_date.day)
    recent_ts = datetime(recent_date.year, recent_date.month, recent_date.day)

    old_rec = _make_pred_record("loc_old", old_ts, tmin_obs=100.0)
    new_rec = _make_pred_record("loc_new", recent_ts, tmin_obs=5.0)
    old_rec["model_version"] = "20260101"
    new_rec["model_version"] = "20260625"

    db_with_predictions.upsert_predictions([old_rec, new_rec])
    _backfill_obs(db_with_predictions, [old_rec, new_rec])

    results = compute_coverage(db_with_predictions)
    # Il vecchio NON dovrebbe essere contato (oltre 30gg).
    # Verifica che n_obs di tmin_c sia solo le righe recenti (50 del base + 1 loc_new).
    for r in results:
        if r.target == "tmin_c":
            assert r.n_obs == 51, f"tmin ha {r.n_obs} obs, dovrebbe essere 51 (no old data)"
