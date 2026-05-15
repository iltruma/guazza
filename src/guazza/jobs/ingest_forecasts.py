"""Job: ingestion previsioni NWP (Open-Meteo) — ogni 6 ore."""

import typer

app = typer.Typer(help="Ingestion previsioni numeriche da Open-Meteo.")


@app.command()
def main(
    location: str = typer.Option(
        ..., "--location", "-l", help="ID location (es. casa_campi)"
    ),
) -> None:
    """Esegui ingestion previsioni per la location specificata. Da implementare in Sprint 1."""
    raise NotImplementedError("Sprint 1")


if __name__ == "__main__":
    app()
