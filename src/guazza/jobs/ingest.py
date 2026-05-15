"""Entry point cron — ingestion dati (SIR + Netatmo + forecasts).

Verra' implementato nello Sprint 1.
"""

from __future__ import annotations

import typer

app = typer.Typer(help="Ingestion dati per Guazza.")


@app.command("realtime")
def cmd_realtime() -> None:
    typer.echo("TODO: ingestion realtime SIR + Netatmo")


@app.command("forecasts")
def cmd_forecasts() -> None:
    typer.echo("TODO: ingestion forecasts NWP")


if __name__ == "__main__":
    app()
