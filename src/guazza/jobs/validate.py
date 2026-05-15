"""Job: calcolo metriche accuracy vs osservazioni — ogni 24 ore."""

import typer

app = typer.Typer(help="Calcola metriche accuracy su osservazioni recenti.")


@app.command()
def main() -> None:
    """Esegui validazione giornaliera. Da implementare in Sprint 2."""
    raise NotImplementedError("Sprint 2")


if __name__ == "__main__":
    app()
