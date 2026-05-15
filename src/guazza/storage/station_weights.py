"""Calcolo e persistenza dei pesi stazioni per ogni location.

Schema di pesi (Alessandrini et al. 2013, calibrato per Toscana):
  - Fonte SIR:     peso base 1.0  (rete validata, storico lungo)
  - Fonte Netatmo: peso base 0.4  (consumer, non validato)
  - Decay distanza: half-weight a 3 km   → exp(-d / 3.0)
  - Penalità quota: half-weight a 100 m  → exp(-Δq / 100.0)

Ricalibrazione prevista dopo 60 giorni di osservazioni reali.

CLI:
    uv run python -m guazza.storage.station_weights refresh
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from loguru import logger

if TYPE_CHECKING:
    from guazza.storage.duckdb_client import DuckDBClient

# ── Costanti del modello di peso ──────────────────────────────────────────────

_HALF_DECAY_KM: float = 3.0    # distanza di half-weight (km)
_HALF_DECAY_ELEV: float = 100.0  # delta quota di half-weight (m)
_SOURCE_WEIGHT: dict[str, float] = {"sir": 1.0, "netatmo": 0.4}

# Path config: sovrascrivibile via CONFIG_DIR env var
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", str(_REPO_ROOT / "config")))


# ── Funzioni di calcolo ───────────────────────────────────────────────────────


def compute_station_weight(
    station_lat: float,
    station_lon: float,
    station_elevation_m: float,
    target_lat: float,
    target_lon: float,
    target_elevation_m: float,
    source: str,
) -> tuple[float, float, float]:
    """Peso di una stazione con coordinate note rispetto a una location target.

    Costanti calibrate su Alessandrini et al. 2013 per Toscana:
    - half-decay distanza: 3 km
    - half-decay quota:    100 m
    Da ricalibrare dopo 60 giorni di dati reali.

    Args:
        source: 'sir' | 'netatmo'

    Returns:
        (weight, distance_km, delta_elev_m)
    """
    base = _SOURCE_WEIGHT.get(source, 1.0)

    # Distanza euclidea approssimata (sufficiente per < 30 km)
    dlat = (station_lat - target_lat) * 111.0
    dlon = (station_lon - target_lon) * 111.0 * math.cos(math.radians(target_lat))
    distance_km = math.sqrt(dlat**2 + dlon**2)

    distance_decay = math.exp(-distance_km / _HALF_DECAY_KM)

    delta_elev = abs(station_elevation_m - target_elevation_m)
    elevation_penalty = math.exp(-delta_elev / _HALF_DECAY_ELEV)

    return base * distance_decay * elevation_penalty, distance_km, delta_elev


def _weight_from_precalc_dist(
    distance_km: float,
    station_elevation_m: float,
    target_elevation_m: float,
    source: str,
) -> tuple[float, float, float]:
    """Variante per stazioni con distanza già calcolata (stazioni Netatmo in config).

    Returns:
        (weight, distance_km, delta_elev_m)
    """
    base = _SOURCE_WEIGHT.get(source, 1.0)
    distance_decay = math.exp(-distance_km / _HALF_DECAY_KM)
    delta_elev = abs(station_elevation_m - target_elevation_m)
    elevation_penalty = math.exp(-delta_elev / _HALF_DECAY_ELEV)
    return base * distance_decay * elevation_penalty, distance_km, delta_elev


# ── Caricamento config ────────────────────────────────────────────────────────


def load_configs(
    config_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Carica locations.yaml e stations.yaml dalla directory config.

    Returns:
        (locations_dict, stations_dict)  — sezione 'locations' e intero stations
    """
    d = config_dir or _DEFAULT_CONFIG_DIR
    with (d / "locations.yaml").open() as f:
        locations_raw = yaml.safe_load(f)
    with (d / "stations.yaml").open() as f:
        stations_raw = yaml.safe_load(f)
    return locations_raw["locations"], stations_raw


# ── Refresh principale ────────────────────────────────────────────────────────


def refresh_station_weights(
    db: DuckDBClient,
    locations: dict[str, Any],
    stations: dict[str, Any],
) -> list[dict[str, Any]]:
    """Ricalcola tutti i pesi stazione→location e li salva in DuckDB.

    Sovrascrive l'intera tabella station_weights ad ogni chiamata.
    Idempotente: sicuro da rieseguire senza duplicati.

    Args:
        db:        DuckDBClient aperto in write mode
        locations: dict da locations.yaml['locations']
        stations:  dict intero da stations.yaml

    Returns:
        Lista dei record inseriti (utile per test e report CLI).
    """
    sir_stations: dict[str, Any] = stations.get("sir_stations", {})

    records: list[dict[str, Any]] = []
    now = datetime.now(tz=timezone.utc)

    for loc_id, loc in locations.items():
        target_lat: float = loc["lat"]
        target_lon: float = loc["lon"]
        target_elev: float = loc["elevation_m"]

        # ── SIR: raccoglie tutti gli ID unici usati da questa location ─────────
        sir_ids: set[str] = set()
        for sensor_list in loc.get("sir_stations", {}).values():
            sir_ids.update(sensor_list)
        if idro_id := loc.get("sir_idro_id"):
            sir_ids.add(idro_id)

        for station_id in sorted(sir_ids):
            s = sir_stations.get(station_id)
            if s is None:
                logger.warning(f"[{loc_id}] SIR {station_id} non in stations.yaml — skip")
                continue

            s_lat = s.get("lat")
            s_lon = s.get("lon")
            if s_lat is None or s_lon is None:
                logger.warning(f"[{loc_id}] SIR {station_id} senza coordinate — skip")
                continue

            # quota_m null (alcune stazioni idro): delta_elev = 0, nessuna penalità
            s_elev: float = s.get("quota_m") or target_elev

            weight, dist_km, delta_elev = compute_station_weight(
                s_lat, s_lon, s_elev,
                target_lat, target_lon, target_elev,
                "sir",
            )
            records.append({
                "station_id": station_id,
                "source": "sir",
                "location_id": loc_id,
                "weight": weight,
                "distance_km": dist_km,
                "delta_elev_m": delta_elev,
                "nome": s.get("nome", station_id),
                "computed_at": now,
            })

        # Netatmo: pesi calcolati dinamicamente nel fetcher (netatmo_realtime.py)
        # e loggati in netatmo_fetch_log. Non precompilati qui.

    if not records:
        logger.warning("Nessun record da inserire in station_weights")
        return records

    # Rimpiazza tutto: idempotente, i pesi non cambiano frequentemente
    db.execute("DELETE FROM station_weights")
    db.executemany(
        """
        INSERT INTO station_weights
            (station_id, source, location_id, weight, distance_km, delta_elev_m, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            [
                r["station_id"], r["source"], r["location_id"],
                r["weight"], r["distance_km"], r["delta_elev_m"],
                r["computed_at"],
            ]
            for r in records
        ],
    )
    logger.info(f"station_weights: {len(records)} record salvati")
    return records


# ── Report ────────────────────────────────────────────────────────────────────


def print_weights_report(
    records: list[dict[str, Any]],
    locations: dict[str, Any],
) -> None:
    """Stampa un report leggibile dei pesi calcolati, raggruppato per location."""
    # Raggruppa per location
    by_loc: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_loc.setdefault(r["location_id"], []).append(r)

    for loc_id in locations:
        loc_records = by_loc.get(loc_id, [])
        loc_label = locations[loc_id].get("label", loc_id)
        print(f"\n{loc_id} — {loc_label}:")

        sir_recs = sorted(
            [r for r in loc_records if r["source"] == "sir"],
            key=lambda r: r["weight"],
            reverse=True,
        )
        netatmo_recs = sorted(
            [r for r in loc_records if r["source"] == "netatmo"],
            key=lambda r: r["weight"],
            reverse=True,
        )

        for r in sir_recs:
            marker = " ✓" if r["weight"] >= 0.5 else ""
            print(
                f"  SIR     {r['station_id']} ({r['nome'][:24]:<24}): "
                f"peso={r['weight']:.3f}  dist={r['distance_km']:.1f}km  "
                f"Δq={r['delta_elev_m']:+.0f}m{marker}"
            )
        for r in netatmo_recs:
            marker = " ✓" if r["weight"] >= 0.2 else ""
            print(
                f"  Netatmo {r['station_id']:<20}         : "
                f"peso={r['weight']:.3f}  dist={r['distance_km']:.2f}km  "
                f"Δq={r['delta_elev_m']:+.0f}m{marker}"
            )

        if not loc_records:
            print("  (nessuna stazione)")


# ── CLI ───────────────────────────────────────────────────────────────────────

import typer  # noqa: E402  (import after stdlib per ruff)

app = typer.Typer(help="Gestione pesi stazioni per location.")

_DB_OPTION = typer.Option(
    os.environ.get("DB_PATH", "/var/lib/guazza/guazza.duckdb"),
    "--db",
    help="Path del file DuckDB",
)
_CFG_OPTION = typer.Option(
    str(_DEFAULT_CONFIG_DIR),
    "--config-dir",
    help="Directory dei file YAML di configurazione",
)


@app.command("refresh")
def cmd_refresh(
    db_path: str = _DB_OPTION,
    config_dir: str = _CFG_OPTION,
) -> None:
    """Ricalcola i pesi stazione→location e salva in DuckDB. Stampa report."""
    from guazza.storage.duckdb_client import DuckDBClient

    locations, stations = load_configs(Path(config_dir))

    with DuckDBClient(db_path=Path(db_path)) as db:
        db.init_schema()
        db.run_migrations()
        records = refresh_station_weights(db, locations, stations)

    print_weights_report(records, locations)
    typer.echo(f"\nTotale: {len(records)} record salvati in station_weights.")


@app.command("show")
def cmd_show(db_path: str = _DB_OPTION) -> None:
    """Mostra i pesi attualmente salvati in DuckDB (senza ricalcolare)."""
    from guazza.storage.duckdb_client import DuckDBClient

    with DuckDBClient(db_path=Path(db_path), read_only=True) as db:
        rows = db.execute(
            """
            SELECT location_id, source, station_id, weight, distance_km, delta_elev_m
            FROM station_weights
            ORDER BY location_id, source DESC, weight DESC
            """
        ).fetchall()

    if not rows:
        typer.echo("station_weights è vuota. Esegui prima: refresh")
        return

    current_loc = None
    for loc_id, source, station_id, weight, dist_km, delta_elev in rows:
        if loc_id != current_loc:
            typer.echo(f"\n{loc_id}:")
            current_loc = loc_id
        marker = " ✓" if (source == "sir" and weight >= 0.5) or (source == "netatmo" and weight >= 0.2) else ""
        typer.echo(
            f"  {source:<8} {station_id:<22} peso={weight:.3f}  "
            f"dist={dist_km:.2f}km  Δq={delta_elev:+.0f}m{marker}"
        )


if __name__ == "__main__":
    app()
