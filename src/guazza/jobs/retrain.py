"""Job: retraining settimanale LightGBM — domenica notte."""

import typer

app = typer.Typer(help="Retraining settimanale modelli LightGBM quantile.")


@app.command()
def main() -> None:
    """Esegui retraining. Da implementare in Sprint 2."""
    raise NotImplementedError("Sprint 2")


if __name__ == "__main__":
    app()
