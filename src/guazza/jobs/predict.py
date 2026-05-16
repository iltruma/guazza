"""Entry point cron — predizioni ML.

Verra' implementato nello Sprint 5.
"""

from __future__ import annotations

import typer

app = typer.Typer(help="Predizioni ML per Guazza.")


@app.command("run")
def cmd_run() -> None:
    typer.echo("TODO: genera predizioni per tutte le location")


if __name__ == "__main__":
    app()
