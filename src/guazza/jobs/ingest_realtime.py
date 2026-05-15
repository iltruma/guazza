"""Job: ingestion real-time (SIR Toscana, idrometria) — ogni ora."""

import typer

app = typer.Typer(help="Ingestion real-time da SIR Toscana e idrometria.")


@app.command()
def main() -> None:
    """Esegui ingestion real-time. Da implementare in Sprint 1."""
    raise NotImplementedError("Sprint 1")


if __name__ == "__main__":
    app()
