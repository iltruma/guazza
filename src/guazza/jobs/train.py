"""Entry point cron — training modello LightGBM + CQR calibration.

Comandi:
  train run   — allena su tutti i dati, salva artefatti
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from guazza._logging import setup_logging
from guazza.jobs._common import DB_OPTION, job_run
from guazza.models import TrainingArtifacts, train_all
from guazza.storage import DuckDBClient

app = typer.Typer(help="Training LightGBM + CQR per Guazza.")

_MODEL_DIR = Path(os.environ.get("MODEL_DIR", "/var/lib/guazza/models"))


@app.callback()
def _callback() -> None:
    setup_logging()


@app.command("run")
def cmd_run(
    db_path:   Path = DB_OPTION,
    model_dir: Path = typer.Option(_MODEL_DIR, "--model-dir", help="Directory artefatti"),
    cal_days:  int  = typer.Option(90,         "--cal-days",  help="Giorni calibration set CQR"),
    dry_run:   bool = typer.Option(False,      "--dry-run",   help="Carica dati ma non allena"),
) -> None:
    """Allena modelli su tutti i dati disponibili e salva artefatti."""
    if dry_run:
        with DuckDBClient(db_path=db_path, read_only=True) as db:
            from guazza.models import load_features
            df = load_features(db)
        typer.echo(f"Dry-run: {len(df)} righe in features_daily. Training saltato.")
        return

    with job_run("job_train_run") as stats:
        with DuckDBClient(db_path=db_path, read_only=True) as db:
            artifacts = train_all(db, model_dir=model_dir, cal_days=cal_days)
        stats.rows = artifacts.n_train
        stats.summary = f"n_train:{artifacts.n_train} n_cal:{artifacts.n_cal}"
        typer.echo(
            f"Training completato: {artifacts.n_train} righe train, "
            f"{artifacts.n_cal} righe cal, "
            f"artefatti in {model_dir}/artifacts.json"
        )
        _print_cqr_summary(artifacts)


def _print_cqr_summary(artifacts: TrainingArtifacts) -> None:
    typer.echo("\nCQR corrections (bucket 0-6h):")
    for target, bundle in artifacts.targets.items():
        corr = bundle.cqr.get("0-6h")
        if corr:
            typer.echo(
                f"  {target:12s}  ci80={corr.ci80:+.3f}  ci90={corr.ci90:+.3f}  n_cal={corr.n_cal}"
            )


if __name__ == "__main__":
    app()
