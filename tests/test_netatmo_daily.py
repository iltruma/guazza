"""Test per netatmo_daily.py — aggregazione realtime → daily."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from guazza.netatmo_daily import aggregate_netatmo_daily
from guazza.storage import DuckDBClient


def _rt(ts: datetime, temp_c: float, humidity_pct: float | None = None) -> dict[str, Any]:
    """Un campione Netatmo realtime (ts UTC naive, come nel DB)."""
    return {
        "source": "netatmo",
        "station_id": "70:ee:50:aa:bb:cc",
        "location_id": "casa_cercina",
        "ts": ts,
        "granularity": "realtime",
        "temp_c": temp_c,
        "humidity_pct": humidity_pct,
    }


def _daily_rows(db: DuckDBClient) -> list[tuple[Any, ...]]:
    return cast(
        list[tuple[Any, ...]],
        db.execute(
            """
            SELECT station_id, location_id, ts, tmin_c, tmax_c, humidity_pct,
                   precip_mm, precip_interval_h
            FROM observations
            WHERE source = 'netatmo' AND granularity = 'daily'
            ORDER BY ts, station_id
            """
        ).fetchall(),
    )


def test_aggregate_basic(tmp_db: Path) -> None:
    """tmin=MIN, tmax=MAX, humidity=AVG; precip resta NULL."""
    samples = [
        _rt(datetime(2026, 6, 1, 6, 0), 9.0, 80.0),
        _rt(datetime(2026, 6, 1, 8, 0), 12.0, 70.0),
        _rt(datetime(2026, 6, 1, 12, 0), 20.0, 50.0),
        _rt(datetime(2026, 6, 1, 14, 0), 22.0, 45.0),
        _rt(datetime(2026, 6, 1, 18, 0), 16.0, 60.0),
        _rt(datetime(2026, 6, 1, 20, 0), 11.0, 75.0),  # 22:00 Roma, ancora il 1 giugno
    ]
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations(samples)
        summary = aggregate_netatmo_daily(db, target_day=date(2026, 6, 1))
        rows = _daily_rows(db)

    assert summary == {"days": 1, "stations": 1, "rows": 1}
    assert len(rows) == 1
    _sid, loc, ts, tmin, tmax, hum, precip, p_int = rows[0]
    assert loc == "casa_cercina"
    assert ts == datetime(2026, 6, 1, 0, 0)  # mezzanotte locale, naive
    assert tmin == 9.0
    assert tmax == 22.0
    assert hum == pytest.approx((80 + 70 + 50 + 45 + 60 + 75) / 6)
    assert precip is None  # precip Netatmo non aggregata (rain_1h sovrapposto)
    assert p_int is None


def test_min_samples_threshold(tmp_db: Path) -> None:
    """Un giorno sotto soglia campioni viene scartato."""
    samples = [
        _rt(datetime(2026, 6, 1, 6, 0), 9.0),
        _rt(datetime(2026, 6, 1, 12, 0), 20.0),
    ]
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations(samples)
        summary = aggregate_netatmo_daily(db, target_day=date(2026, 6, 1), min_samples=6)
        rows = _daily_rows(db)

    assert summary["stations"] == 0
    assert rows == []


def test_idempotent(tmp_db: Path) -> None:
    """Due run producono una sola riga daily (upsert idempotente)."""
    samples = [_rt(datetime(2026, 6, 1, h, 0), float(h)) for h in range(6, 18, 2)]
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations(samples)
        aggregate_netatmo_daily(db, target_day=date(2026, 6, 1), min_samples=3)
        aggregate_netatmo_daily(db, target_day=date(2026, 6, 1), min_samples=3)
        rows = _daily_rows(db)

    assert len(rows) == 1


def test_local_day_boundary_europe_rome(tmp_db: Path) -> None:
    """Il confine giorno usa Europe/Rome: 23:30 UTC (estate) cade il giorno dopo."""
    samples = [
        # 2026-06-01 → Rome estate UTC+2
        _rt(datetime(2026, 6, 1, 8, 0), 15.0),
        _rt(datetime(2026, 6, 1, 10, 0), 18.0),
        _rt(datetime(2026, 6, 1, 12, 0), 22.0),
        # 23:30 UTC = 01:30 del 2 giugno a Roma → giorno locale 2026-06-02
        _rt(datetime(2026, 6, 1, 23, 30), 14.0),
        _rt(datetime(2026, 6, 1, 23, 45), 13.0),
        _rt(datetime(2026, 6, 2, 0, 30), 12.0),
    ]
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations(samples)
        aggregate_netatmo_daily(db, min_samples=3)  # tutti i giorni
        rows = _daily_rows(db)

    by_day = {r[2]: r for r in rows}
    assert datetime(2026, 6, 1, 0, 0) in by_day
    assert datetime(2026, 6, 2, 0, 0) in by_day
    # Il giorno 1 vede solo i 3 campioni diurni (min 15, max 22)
    assert by_day[datetime(2026, 6, 1, 0, 0)][3] == 15.0
    assert by_day[datetime(2026, 6, 1, 0, 0)][4] == 22.0
    # Il giorno 2 raccoglie i campioni di confine (min 12, max 14)
    assert by_day[datetime(2026, 6, 2, 0, 0)][3] == 12.0
    assert by_day[datetime(2026, 6, 2, 0, 0)][4] == 14.0


def test_dry_run_writes_nothing(tmp_db: Path) -> None:
    samples = [_rt(datetime(2026, 6, 1, h, 0), float(h)) for h in range(6, 18, 2)]
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations(samples)
        summary = aggregate_netatmo_daily(
            db, target_day=date(2026, 6, 1), min_samples=3, dry_run=True
        )
        rows = _daily_rows(db)

    assert summary["rows"] == 0
    assert summary["stations"] == 1  # conteggiate ma non scritte
    assert rows == []


def test_training_target_excludes_netatmo(tmp_db: Path) -> None:
    """Garanzia: le righe daily Netatmo non hanno source 'sir_toscana'
    (features.py filtra su quello → Netatmo resta fuori dal training)."""
    samples = [_rt(datetime(2026, 6, 1, h, 0), float(h)) for h in range(6, 18, 2)]
    with DuckDBClient(db_path=tmp_db) as db:
        db.init_schema()
        db.upsert_sir_observations(samples)
        aggregate_netatmo_daily(db, target_day=date(2026, 6, 1), min_samples=3)
        sir_daily = db.execute(
            "SELECT COUNT(*) FROM observations "
            "WHERE source = 'sir_toscana' AND granularity = 'daily'"
        ).fetchone()

    assert sir_daily is not None
    assert sir_daily[0] == 0
