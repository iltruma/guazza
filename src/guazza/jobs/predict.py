"""Job: inference ML + Decision Logic Engine — ogni 6 ore."""

import typer

app = typer.Typer(help="Inference LightGBM quantile + DLE per tutte le location.")


@app.command()
def main() -> None:
    """Esegui inference e aggiorna JSON output. Da implementare in Sprint 2."""
    raise NotImplementedError("Sprint 2")


if __name__ == "__main__":
    app()
