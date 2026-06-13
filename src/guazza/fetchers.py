"""CLI dei fetcher meteo Guazza.

La logica vive nei moduli di dominio:
  - guazza.fetch_sir       — SIR Toscana (storico CSV, realtime JSON, bulk)
  - guazza.fetch_openmeteo — Open-Meteo (forecast live, historical, multi-lead)
  - guazza.fetch_netatmo   — Netatmo realtime + QC
  - guazza.fetch_arpat     — ARPAT qualità aria (NRT + bollettino)
  - guazza.fetch_common    — costanti e helper HTTP condivisi

CLI:
    uv run python -m guazza.fetchers sir-historical --station TOS01001215 --sensor termo_csv
    uv run python -m guazza.fetchers netatmo --location casa_campi
"""

from __future__ import annotations

from pathlib import Path

import typer

from guazza._paths import DEFAULT_DB_PATH
from guazza.fetch_netatmo import fetch_netatmo_all_locations
from guazza.fetch_sir import fetch_sir_historical, fetch_sir_realtime
from guazza.storage import DuckDBClient

app = typer.Typer(help="Fetcher meteo per Guazza.")

_DB_OPTION = typer.Option(str(DEFAULT_DB_PATH), "--db", help="Path del file DuckDB")


@app.command("sir-historical")
def cmd_sir_historical(
    station: str = typer.Option(..., help="ID stazione SIR"),
    sensor: str = typer.Option(..., help="Tipo sensore (termo_csv, pluvio0_24, ...)"),
    location: str = typer.Option("", help="ID location Guazza"),
) -> None:
    """Scarica storico CSV SIR e stampa le prime righe."""
    rows = fetch_sir_historical(station, sensor, location)
    typer.echo(f"Righe recuperate: {len(rows)}")
    for r in rows[:5]:
        typer.echo(str(r))


@app.command("sir-realtime")
def cmd_sir_realtime(
    station: str = typer.Option(..., help="ID stazione SIR"),
) -> None:
    """Recupera realtime SIR e stampa il record wide."""
    record = fetch_sir_realtime(station)
    typer.echo(str(record))


@app.command("netatmo")
def cmd_netatmo(
    db_path: str = _DB_OPTION,
    location: str | None = typer.Option(None, "--location", help="Solo questa location"),
) -> None:
    """Fetch Netatmo per tutte le location e salva in DuckDB."""
    with DuckDBClient(db_path=Path(db_path)) as db:
        db.init_schema()
        results = fetch_netatmo_all_locations(db, target_location=location)

    total_stations = sum(len(v) for v in results.values())
    total_ok = sum(sum(1 for sd in v if sd.qc_pass) for v in results.values())
    typer.echo(f"\nFetch completato: {total_stations} stazioni totali, {total_ok} QC-pass")
    for loc_id, stations in results.items():
        ok = sum(1 for sd in stations if sd.qc_pass)
        typer.echo(f"  {loc_id}: {len(stations)} stazioni, {ok} QC-pass")


if __name__ == "__main__":
    app()
