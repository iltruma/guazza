"""Entry point cron — backup DuckDB su R2.

Verra' implementato nello Sprint 1.
"""

from __future__ import annotations

import typer

app = typer.Typer(help="Backup Guazza.")


@app.command("run")
def cmd_run() -> None:
    typer.echo("TODO: backup guazza.duckdb su Cloudflare R2")


if __name__ == "__main__":
    app()
