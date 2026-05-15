"""Job: backup DuckDB su Cloudflare R2 — ogni 24 ore."""

import typer

app = typer.Typer(help="Backup notturno DuckDB su Cloudflare R2.")


@app.command()
def main() -> None:
    """Esegui backup. Da implementare in Sprint 1."""
    raise NotImplementedError("Sprint 1")


if __name__ == "__main__":
    app()
