"""Test per skill_history — append giornaliero e dump JSON per il frontend.

Logica testata in isolamento (no DB), coprendo:
- Costruzione delle righe (forecast vs actual) da DataFrame mockati
- Dump JSON time series correttamente allineato per location/variable
- Edge case: location senza obs, source mancante, date con buchi
- Atomic write: il file JSON finale è sempre valido anche se il tmp persiste
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import duckdb
import pytest

from guazza.skill_history import (
    ALL_SOURCES,
    LEAD_H,
    NWP_SOURCES,
    VARS,
    _collect_rows,
    atomic_write_json,
    dump_payload,
)

# ── fixtures: query SQL mockate in DataFrame ────────────────────────────────


def _mk_guazza(loc: str, tmin: float, tmax: float, precip: float) -> tuple:
    return (loc, tmin, tmax, precip)


def _mk_nwp(src: str, loc: str, tmin: float, tmax: float, precip: float) -> tuple:
    return (src, loc, tmin, tmax, precip)


def _mk_actual(loc: str, tmin: float, tmax: float, precip: float) -> tuple:
    return (loc, tmin, tmax, precip)


# ── _collect_rows ──────────────────────────────────────────────────────────


def test_collect_rows_creates_one_per_source_per_var() -> None:
    """Per 1 location × 1 actual × 7 sources × 3 vars = 21 righe."""
    class FakeResult:
        def __init__(self, rows: list) -> None:
            self._rows = rows
        def fetchall(self) -> list:
            return self._rows

    class FakeCon:
        def execute(self, sql: str) -> FakeResult:  # noqa: ARG002
            if "FROM obs_weighted_daily" in sql:
                return FakeResult([("casa_campi", 10.0, 20.0, 1.0)])
            if "FROM predictions" in sql:
                return FakeResult([("casa_campi", 10.5, 19.5, 0.8)])
            if "FROM forecasts" in sql:
                # 4 NWP, tutti per casa_campi
                return FakeResult([
                    ("open_meteo_ecmwf_ifs", "casa_campi", 11.0, 21.0, 0.0),
                    ("open_meteo_icon_eu", "casa_campi", 11.5, 20.5, 0.5),
                    ("open_meteo_arome_france", "casa_campi", 10.8, 20.2, 0.1),
                    ("open_meteo_italia_meteo_arpae_icon_2i", "casa_campi", 10.9, 20.3, 0.4),
                ])
            return FakeResult([])

    rows = _collect_rows(FakeCon(), date(2026, 6, 27))
    # 1 loc × 3 var × (1 Guazza + 4 NWP) = 15
    assert len(rows) == 3 * (1 + len(NWP_SOURCES))
    # Tupla = (loc, date, source, variable, forecast, actual, abs_err).
    # lead_h NON è nella tupla: è cablato nello SQL (LEAD_H = 24).
    assert all(len(r) == 7 for r in rows)
    # Guazza Tmin: forecast 10.5, actual 10.0 → |err| = 0.5
    guazza_tmin = next(r for r in rows if r[2] == "guazza" and r[3] == "tmin_c")
    assert guazza_tmin[4] == 10.5  # forecast
    assert guazza_tmin[5] == 10.0  # actual
    assert guazza_tmin[6] == 0.5   # abs_error


def test_collect_rows_skips_when_actual_missing() -> None:
    """Se obs_weighted_daily è vuoto, nessuna riga viene creata."""
    class FakeResult:
        def fetchall(self) -> list: return []
    class FakeCon:
        def execute(self, sql: str) -> FakeResult:  # noqa: ARG002
            return FakeResult()

    rows = _collect_rows(FakeCon(), date(2026, 6, 27))
    assert rows == []


def test_collect_rows_skips_when_actual_has_null_var() -> None:
    """actual con tmin_c NULL non genera righe per tmin_c, ma sì per tmax_c."""
    class FakeResult:
        def __init__(self, rows: list) -> None: self._rows = rows
        def fetchall(self) -> list: return self._rows
    class FakeCon:
        def execute(self, sql: str) -> FakeResult:  # noqa: ARG002
            if "FROM obs_weighted_daily" in sql:
                return FakeResult([("casa_campi", None, 20.0, 1.0)])
            if "FROM predictions" in sql:
                return FakeResult([("casa_campi", 10.0, 19.0, 0.5)])
            if "FROM forecasts" in sql:
                return FakeResult([("open_meteo_ecmwf_ifs", "casa_campi", 11.0, 21.0, 0.0)])
            return FakeResult([])

    rows = _collect_rows(FakeCon(), date(2026, 6, 27))
    vars_seen = {r[3] for r in rows}
    assert "tmin_c" not in vars_seen
    assert "tmax_c" in vars_seen
    assert "precip_mm" in vars_seen


def test_collect_rows_skips_nwp_missing_for_location() -> None:
    """Un NWP che non ha forecast per la location viene saltato."""
    class FakeResult:
        def __init__(self, rows: list) -> None: self._rows = rows
        def fetchall(self) -> list: return self._rows
    class FakeCon:
        def execute(self, sql: str) -> FakeResult:  # noqa: ARG002
            if "FROM obs_weighted_daily" in sql:
                return FakeResult([("casa_campi", 10.0, 20.0, 1.0)])
            if "FROM predictions" in sql:
                return FakeResult([("casa_campi", 10.5, 19.5, 0.8)])
            if "FROM forecasts" in sql:
                return FakeResult([("open_meteo_ecmwf_ifs", "casa_campi", 11.0, 21.0, 0.0)])
            return FakeResult([])

    rows = _collect_rows(FakeCon(), date(2026, 6, 27))
    sources = {r[2] for r in rows}
    assert sources == {"guazza", "open_meteo_ecmwf_ifs"}


# ── _dump_payload ───────────────────────────────────────────────────────────


def _seed_db(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    """Crea un DB in-memory con la tabella skill_history_daily e dati minimi."""
    con = duckdb.connect(str(tmp_path / "test.duckdb"))
    con.execute("""
        CREATE TABLE skill_history_daily (
            location_id    VARCHAR  NOT NULL,
            target_date    DATE     NOT NULL,
            source         VARCHAR  NOT NULL,
            variable       VARCHAR  NOT NULL,
            lead_h         SMALLINT NOT NULL,
            forecast_value DOUBLE,
            actual_value   DOUBLE,
            abs_error      DOUBLE,
            generated_at   TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (location_id, target_date, source, variable, lead_h)
        )
    """)
    # 3 date, 1 location, 2 var, 3 source
    rows = []
    for d in ("2026-06-25", "2026-06-26", "2026-06-27"):
        for var in ("tmin_c", "tmax_c"):
            for src in ("guazza", "open_meteo_ecmwf_ifs", "open_meteo_icon_eu"):
                fc = 10.0 if src == "guazza" else 11.0
                ac = 10.5
                err = abs(fc - ac)
                rows.append(("casa_campi", d, src, var, 24, fc, ac, err))
    con.executemany(
        "INSERT INTO skill_history_daily VALUES (?,?,?,?,?,?,?,?,current_timestamp)",
        rows,
    )
    return con


def test_dump_payload_structure() -> None:
    """Verifica la forma del JSON: locations → variable → dates, actual, per-source."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        con = _seed_db(Path(tmp))
        try:
            payload = dump_payload(con)
        finally:
            con.close()

    assert payload["lead_h"] == LEAD_H
    assert set(payload["sources"]) == set(ALL_SOURCES)
    assert set(payload["variables"]) == set(VARS)
    assert payload["min_date"] == "2026-06-25"
    assert payload["max_date"] == "2026-06-27"
    assert "casa_campi" in payload["locations"]

    tmin = payload["locations"]["casa_campi"]["tmin_c"]
    assert tmin["dates"] == ["2026-06-25", "2026-06-26", "2026-06-27"]
    assert tmin["actual"] == [10.5, 10.5, 10.5]
    assert tmin["guazza"] == [10.0, 10.0, 10.0]
    assert tmin["open_meteo_ecmwf_ifs"] == [11.0, 11.0, 11.0]
    # Source assente (mai inserito) → null per ogni data
    assert tmin["open_meteo_arome_france"] == [None, None, None]


def test_dump_payload_handles_empty_table() -> None:
    """Tabella vuota → payload con locations={} e date=None."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        empty_path = Path(tmp) / "empty.duckdb"
        con = duckdb.connect(str(empty_path))
        con.execute("""
            CREATE TABLE skill_history_daily (
                location_id VARCHAR, target_date DATE, source VARCHAR,
                variable VARCHAR, lead_h SMALLINT,
                forecast_value DOUBLE, actual_value DOUBLE, abs_error DOUBLE,
                generated_at TIMESTAMP,
                PRIMARY KEY (location_id, target_date, source, variable, lead_h)
            )
        """)
        try:
            payload = dump_payload(con)
        finally:
            con.close()

    assert payload["locations"] == {}
    assert payload["min_date"] is None
    assert payload["max_date"] is None


# ── _atomic_write_json ──────────────────────────────────────────────────────


def test_atomic_write_replaces_existing(tmp_path: Path) -> None:
    """Scrive il JSON in tmp e poi lo sposta sul path finale (no file parziale)."""
    out = tmp_path / "out.json"
    atomic_write_json(out, {"a": 1})
    assert json.loads(out.read_text()) == {"a": 1}
    # Nessun .tmp residuo
    assert not (tmp_path / "out.json.tmp").exists()
    # Riscrittura: il contenuto è sostituito, non appendi
    atomic_write_json(out, {"a": 2})
    assert json.loads(out.read_text()) == {"a": 2}


def test_atomic_write_creates_parent_dirs(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "out.json"
    atomic_write_json(out, {"x": True})
    assert out.exists()


# ── smoke: il modulo è importabile e ha le funzioni usate dalla pipeline ───


def test_public_functions_match_pipeline_usage() -> None:
    import guazza.skill_history as mod

    assert hasattr(mod, "append_one")
    assert hasattr(mod, "dump_payload")
    assert hasattr(mod, "atomic_write_json")

    import inspect
    for fn in (mod.append_one, mod.dump_payload):
        sig = inspect.signature(fn)
        params = list(sig.parameters.keys())
        assert params[0] == "con", f"manca connessione DuckDB in {fn.__name__}: {params}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
