"""Calcolo e persistenza dei pesi stazioni per ogni location.

Schema di pesi (Alessandrini et al. 2013, calibrato per Toscana):
  - Fonte SIR:     peso base 1.0  (rete validata, storico lungo)
  - Fonte Netatmo: peso base 0.4  (consumer, non validato)
  - Decay distanza: half-weight a 3 km   → exp(-d / 3.0)
  - Penalità quota: half-weight a 100 m  → exp(-Δq / 100.0)

Ricalibrazione prevista dopo 60 giorni di osservazioni reali.

CLI:
    uv run python -m guazza.weights refresh
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import typer
import yaml
from loguru import logger

from guazza._logging import setup_logging
from guazza._paths import DEFAULT_CONFIG_DIR, DEFAULT_DB_PATH
from guazza.storage import DuckDBClient

_HALF_DECAY_KM: float = 3.0
_HALF_DECAY_ELEV: float = 100.0
_SOURCE_WEIGHT: dict[str, float] = {"sir": 1.0, "netatmo": 0.4}

# Ring thresholds per feature upstream pluviometriche
_RING_THRESHOLDS: list[tuple[str, float]] = [
    ("ring1", 20.0),   # 0–20 km: locale / stesso sistema di valli
    ("ring2", 50.0),   # 20–50 km: sub-regionale
    ("ring3", 100.0),  # 50–100 km: Appennino/Versilia — da popolare con stazioni esterne
]

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

    Returns:
        (weight, distance_km, delta_elev_m)
    """
    base = _SOURCE_WEIGHT.get(source, 1.0)
    dlat = (station_lat - target_lat) * 111.0
    dlon = (station_lon - target_lon) * 111.0 * math.cos(math.radians(target_lat))
    distance_km = math.sqrt(dlat**2 + dlon**2)
    distance_decay = math.exp(-distance_km / _HALF_DECAY_KM)
    delta_elev = abs(station_elevation_m - target_elevation_m)
    elevation_penalty = math.exp(-delta_elev / _HALF_DECAY_ELEV)
    return base * distance_decay * elevation_penalty, distance_km, delta_elev


def _ring_label(distance_km: float) -> str | None:
    for label, threshold in _RING_THRESHOLDS:
        if distance_km <= threshold:
            return label
    return None  # oltre 100km, esclusa


def refresh_upstream_rings(
    db: DuckDBClient,
    locations: dict[str, Any],
    stations: dict[str, Any],
) -> list[dict[str, Any]]:
    """Assegna le stazioni pluvio SIR ai ring per ogni location in base alla distanza.

    Tutte le stazioni in stations.yaml con sensore pluviometro vengono candidate.
    Ring assignment: ring1 ≤20km, ring2 20-50km, ring3 50-100km.
    Popola upstream_ring_station (DELETE+INSERT idempotente).
    """
    sir_stations: dict[str, Any] = stations.get("sir_stations", {})
    records: list[dict[str, Any]] = []

    for loc_id, loc in locations.items():
        target_lat: float = loc["lat"]
        target_lon: float = loc["lon"]
        target_elev: float = loc["elevation_m"]

        for station_id, s in sir_stations.items():
            if "pluviometro" not in s.get("sensors", []):
                continue
            s_lat = s.get("lat")
            s_lon = s.get("lon")
            if s_lat is None or s_lon is None:
                continue
            s_elev: float = s.get("quota_m") or target_elev
            _, dist_km, _ = compute_station_weight(
                s_lat, s_lon, s_elev,
                target_lat, target_lon, target_elev,
                "sir",
            )
            ring = _ring_label(dist_km)
            if ring is None:
                continue
            records.append({
                "station_id": station_id,
                "location_id": loc_id,
                "ring_label": ring,
                "distance_km": dist_km,
            })

    db.execute("DELETE FROM upstream_ring_station")
    if records:
        df = pd.DataFrame(records, columns=["station_id", "location_id", "ring_label", "distance_km"])
        db.register_df("_stg_rings", df)
        db.execute(
            "INSERT INTO upstream_ring_station (station_id, location_id, ring_label, distance_km)"
            " SELECT station_id, location_id, ring_label, distance_km FROM _stg_rings"
        )
        db.unregister_df("_stg_rings")
    logger.info(f"upstream_ring_station: {len(records)} record salvati")
    return records


def load_configs(
    config_dir: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Carica locations.yaml e stations.yaml dalla directory config."""
    d = config_dir or DEFAULT_CONFIG_DIR
    with (d / "locations.yaml").open() as f:
        locations_raw = yaml.safe_load(f)
    with (d / "stations.yaml").open() as f:
        stations_raw = yaml.safe_load(f)
    return locations_raw["locations"], stations_raw


def refresh_station_weights(
    db: DuckDBClient,
    locations: dict[str, Any],
    stations: dict[str, Any],
) -> list[dict[str, Any]]:
    """Ricalcola tutti i pesi stazione→location e li salva in DuckDB.

    Sovrascrive l'intera tabella station_weights ad ogni chiamata.
    """
    sir_stations: dict[str, Any] = stations.get("sir_stations", {})
    records: list[dict[str, Any]] = []
    now = datetime.now(tz=UTC)

    for loc_id, loc in locations.items():
        target_lat: float = loc["lat"]
        target_lon: float = loc["lon"]
        target_elev: float = loc["elevation_m"]

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
            s_elev: float = s["quota_m"] if s.get("quota_m") is not None else target_elev
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



    if not records:
        logger.warning("Nessun record da inserire in station_weights")
        return records

    db.execute("DELETE FROM station_weights")
    df = pd.DataFrame(
        [[r["station_id"], r["source"], r["location_id"],
          r["weight"], r["distance_km"], r["delta_elev_m"], r["computed_at"]]
         for r in records],
        columns=["station_id", "source", "location_id", "weight", "distance_km", "delta_elev_m", "computed_at"],
    )
    db.register_df("_stg_weights", df)
    db.execute(
        "INSERT INTO station_weights"
        " (station_id, source, location_id, weight, distance_km, delta_elev_m, computed_at)"
        " SELECT station_id, source, location_id, weight, distance_km, delta_elev_m, computed_at"
        " FROM _stg_weights"
    )
    db.unregister_df("_stg_weights")
    logger.info(f"station_weights: {len(records)} record salvati")
    return records


# ── CLI ───────────────────────────────────────────────────────────────────────

app = typer.Typer(help="Gestione pesi stazioni per location.")


def print_weights_report(
    records: list[dict[str, Any]],
    locations: dict[str, Any],
) -> None:
    """Stampa un report leggibile dei pesi calcolati, raggruppato per location."""
    by_loc: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_loc.setdefault(r["location_id"], []).append(r)

    for loc_id in locations:
        loc_records = by_loc.get(loc_id, [])
        loc_label = locations[loc_id].get("label", loc_id)
        typer.echo(f"\n{loc_id} — {loc_label}:")

        sir_recs = sorted(
            [r for r in loc_records if r["source"] == "sir"],
            key=lambda r: r["weight"],
            reverse=True,
        )
        for r in sir_recs:
            marker = " ✓" if r["weight"] >= 0.5 else ""
            typer.echo(
                f"  SIR     {r['station_id']} ({r['nome'][:24]:<24}): "
                f"peso={r['weight']:.3f}  dist={r['distance_km']:.1f}km  "
                f"Δq={r['delta_elev_m']:+.0f}m{marker}"
            )
        if not loc_records:
            typer.echo("  (nessuna stazione)")

_DB_OPTION = typer.Option(str(DEFAULT_DB_PATH), "--db", help="Path del file DuckDB")
_CFG_OPTION = typer.Option(
    str(DEFAULT_CONFIG_DIR),
    "--config-dir",
    help="Directory dei file YAML di configurazione",
)


@app.callback()
def _callback() -> None:
    setup_logging()


@app.command("refresh")
def cmd_refresh(
    db_path: str = _DB_OPTION,
    config_dir: str = _CFG_OPTION,
) -> None:
    """Ricalcola i pesi stazione→location e salva in DuckDB. Stampa report."""
    locations, stations = load_configs(Path(config_dir))
    with DuckDBClient(db_path=Path(db_path)) as db:
        db.init_schema()
        records = refresh_station_weights(db, locations, stations)
        ring_records = refresh_upstream_rings(db, locations, stations)

    print_weights_report(records, locations)
    typer.echo(f"\nTotale: {len(records)} record salvati in station_weights.")
    typer.echo(f"Totale: {len(ring_records)} record salvati in upstream_ring_station.")


@app.command("show")
def cmd_show(db_path: str = _DB_OPTION) -> None:
    """Mostra i pesi attualmente salvati in DuckDB (senza ricalcolare)."""
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
        marker = " ✓" if (source == "sir" and weight >= 0.5) else ""
        typer.echo(
            f"  {source:<8} {station_id:<22} peso={weight:.3f}  "
            f"dist={dist_km:.2f}km  Δq={delta_elev:+.0f}m{marker}"
        )


if __name__ == "__main__":
    app()
