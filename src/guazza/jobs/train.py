"""Entry point cron — training modello LightGBM + CQR calibration.

Comandi:
  train run   — allena su tutti i dati, salva artefatti
  train eval  — walk-forward CV, stampa metriche
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from guazza._logging import setup_logging
from guazza.models import TrainingArtifacts, train_all, walk_forward_cv
from guazza.storage import DuckDBClient

app = typer.Typer(help="Training LightGBM + CQR per Guazza.")

_DB_PATH    = Path(os.environ.get("DB_PATH",    "/var/lib/guazza/guazza.duckdb"))
_MODEL_DIR  = Path(os.environ.get("MODEL_DIR",  "/var/lib/guazza/models"))


@app.command("run")
def cmd_run(
    db_path:   Path = typer.Option(_DB_PATH,   "--db",        help="Path DuckDB"),
    model_dir: Path = typer.Option(_MODEL_DIR, "--model-dir", help="Directory artefatti"),
    cal_days:  int  = typer.Option(90,         "--cal-days",  help="Giorni calibration set CQR"),
    dry_run:   bool = typer.Option(False,      "--dry-run",   help="Carica dati ma non allena"),
) -> None:
    """Allena modelli su tutti i dati disponibili e salva artefatti."""
    setup_logging()

    with DuckDBClient(db_path=db_path, read_only=True) as db:
        if dry_run:
            from guazza.models import load_features
            df = load_features(db)
            typer.echo(f"Dry-run: {len(df)} righe in features_daily. Training saltato.")
            return

        artifacts = train_all(db, model_dir=model_dir, cal_days=cal_days)

    typer.echo(
        f"Training completato: {artifacts.n_train} righe train, "
        f"{artifacts.n_cal} righe cal, "
        f"artefatti in {model_dir}/artifacts.pkl"
    )
    _print_cqr_summary(artifacts)


@app.command("eval")
def cmd_eval(
    db_path:    Path = typer.Option(_DB_PATH,  "--db",       help="Path DuckDB"),
    n_splits:   int  = typer.Option(4,         "--splits",   help="Numero di fold CV"),
    embargo:    int  = typer.Option(7,         "--embargo",  help="Giorni embargo tra train e test"),
) -> None:
    """Walk-forward CV: stampa MAE, CRPS, coverage e skill score."""
    setup_logging()

    with DuckDBClient(db_path=db_path, read_only=True) as db:
        results = walk_forward_cv(db, n_splits=n_splits, embargo_days=embargo)

    if results.empty:
        typer.echo("Nessun risultato CV — dati insufficienti.")
        raise typer.Exit(1)

    # Riepilogo aggregato per target
    summary = (
        results.groupby("target")
        .agg(
            mae=("mae", "mean"),
            crps=("crps", "mean"),
            coverage_80=("coverage_80", "mean"),
            coverage_90=("coverage_90", "mean"),
            skill_mae=("skill_mae", "mean"),
            n_test=("n_test", "sum"),
        )
        .round(3)
    )

    typer.echo("\n=== Walk-forward CV — metriche aggregate ===")
    typer.echo(summary.to_string())

    typer.echo("\n=== Dettaglio per fold ===")
    cols = ["split", "test_start", "target", "mae", "crps", "coverage_80", "coverage_90", "skill_mae"]
    typer.echo(results[cols].to_string(index=False))


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
