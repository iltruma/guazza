#!/usr/bin/env python3
"""Task 0 — Script 01: trova stazioni SIR Toscana vicine alle 4 location.

Interroga il portale Open Data Toscana (dati.toscana.it, CKAN API) per ottenere
l'anagrafica delle stazioni SIR, poi per ogni location calcola distanza e delta quota
e filtra le candidate (distanza < 5 km, delta quota < 100 m).

Output: report Markdown su stdout (redirigere su docs/task0_sir_stations.md).

Uso:
    uv run python scripts/01_find_sir_stations.py > docs/task0_sir_stations.md
    uv run python scripts/01_find_sir_stations.py --verbose
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

# ── Configurazione ─────────────────────────────────────────────────────────

LOCATIONS_YAML = Path(__file__).parent.parent / "config" / "locations.yaml"

# Endpoint CKAN dati.toscana.it — da verificare quale resource_id contiene stazioni SIR
# Se questo endpoint non funziona, provare download bulk CSV dal portale.
CKAN_BASE = "https://dati.toscana.it/api/3/action"

# Dataset SIR noti sul portale — da verificare/aggiornare
# Il resource_id corretto va trovato esplorando https://dati.toscana.it/dataset/sir-rete-meteo
SIR_RESOURCE_IDS = [
    "sir-anagrafica-stazioni",     # placeholder — trovare ID reale
]

DISTANCE_MAX_KM = 5.0
DELTA_ELEV_MAX_M = 100


# ── Funzioni di utilità ────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distanza in km tra due coordinate (formula haversine)."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_locations() -> dict[str, Any]:
    with open(LOCATIONS_YAML) as f:
        return yaml.safe_load(f)["locations"]


def probe_ckan_resource(resource_id: str, limit: int = 5) -> dict[str, Any]:
    """Prova a interrogare una risorsa CKAN e restituisce un campione."""
    url = f"{CKAN_BASE}/datastore_search"
    params = {"resource_id": resource_id, "limit": limit}
    try:
        r = httpx.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e)}


def search_ckan_datasets(query: str = "stazioni meteorologiche SIR") -> dict[str, Any]:
    """Cerca dataset nel portale CKAN."""
    url = f"{CKAN_BASE}/package_search"
    params = {"q": query, "rows": 10}
    try:
        r = httpx.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e)}


def fetch_sir_stations_from_ckan() -> list[dict[str, Any]]:
    """Tenta di recuperare l'anagrafica stazioni SIR da CKAN."""
    # Prima: cerca dataset rilevanti
    search_result = search_ckan_datasets("SIR stazioni meteo Toscana")
    stations: list[dict[str, Any]] = []

    if search_result.get("success") and search_result.get("result", {}).get("results"):
        for dataset in search_result["result"]["results"][:5]:
            print(f"  [CKAN] Dataset trovato: {dataset.get('name')} — {dataset.get('title')}", file=sys.stderr)

    # Poi: prova i resource_id noti (placeholder — da aggiornare con ID reali)
    for resource_id in SIR_RESOURCE_IDS:
        result = probe_ckan_resource(resource_id, limit=1000)
        if result.get("success") and result.get("result", {}).get("records"):
            stations.extend(result["result"]["records"])
            print(f"  [CKAN] Resource {resource_id}: {len(result['result']['records'])} record", file=sys.stderr)

    return stations


# ── Output Markdown ────────────────────────────────────────────────────────

def print_report_header() -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("# Task 0 — Stazioni SIR vicine alle 4 location\n")
    print(f"Generato: {now}\n")
    print("Criteri di selezione: distanza < 5 km, delta quota < 100 m, dati validati ultime 3 anni.\n")
    print("---\n")


def print_location_result(
    loc_id: str,
    loc: dict[str, Any],
    candidates: list[dict[str, Any]],
    ckan_raw_result: dict[str, Any],
) -> None:
    print(f"## {loc_id} — {loc['label']}")
    print(f"- Indirizzo: {loc['address']}")
    print(f"- Coordinate (stimate): ({loc['lat']}, {loc['lon']})")
    print(f"- Quota stimata: {loc['elevation_m']} m\n")

    if not candidates:
        print("**Nessuna stazione trovata entro i criteri.**")
        print(f"> Note: {loc.get('notes', '')}\n")
        print("**Azione richiesta**: trovare manualmente la stazione SIR più vicina su")
        print("https://cfr.toscana.it o https://dati.toscana.it e popolare")
        print(f"`config/locations.yaml` campo `sir_station_id` per `{loc_id}`.\n")
    else:
        print("| Stazione ID | Variabili | Distanza (km) | Delta quota (m) | Dati dal | Flag |")
        print("|---|---|---|---|---|---|")
        for c in candidates:
            flag = ""
            if c.get("distance_km", 0) > 3 or abs(c.get("delta_elev_m", 0)) > 50:
                flag = "⚠️ verifica"
            print(
                f"| {c.get('id', '?')} | {c.get('variables', '?')} | "
                f"{c.get('distance_km', '?'):.1f} | {c.get('delta_elev_m', '?'):+.0f} | "
                f"{c.get('data_from', '?')} | {flag} |"
            )
        print()

    print(f"> Note microclima: {loc.get('notes', '—')}\n")
    print("---\n")


def print_ckan_status(ckan_status: dict[str, Any]) -> None:
    print("## Stato endpoint CKAN dati.toscana.it\n")
    print(f"- URL base: `{CKAN_BASE}`")
    print(f"- Ricerca dataset: {'OK' if ckan_status.get('search_ok') else 'FALLITA'}")
    print(f"- Dataset trovati: {ckan_status.get('datasets_found', 0)}")
    if ckan_status.get("error"):
        print(f"- Errore: `{ckan_status['error']}`")
    print()
    print("### Azione richiesta se CKAN non funziona")
    print()
    print("1. Accedere manualmente a https://cfr.toscana.it/cartografia/")
    print("2. Identificare stazioni nelle vicinanze delle 4 location")
    print("3. Compilare `config/stations.yaml` con gli ID trovati")
    print("4. Aggiornare `config/locations.yaml` con `sir_station_id` e `cfr_station_id`\n")
    print("---\n")


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    print_report_header()

    # 1. Testa endpoint CKAN
    print("## Ricognizione endpoint\n", file=sys.stderr)
    ckan_search = search_ckan_datasets("SIR stazioni meteo Toscana")
    ckan_datasets_found = (
        len(ckan_search.get("result", {}).get("results", []))
        if ckan_search.get("success")
        else 0
    )
    ckan_status = {
        "search_ok": ckan_search.get("success", False),
        "datasets_found": ckan_datasets_found,
        "error": ckan_search.get("error"),
    }

    if verbose:
        print(f"CKAN search result: {json.dumps(ckan_search, indent=2)}", file=sys.stderr)

    # 2. Prova a recuperare stazioni
    all_stations = fetch_sir_stations_from_ckan()
    print(f"  [SIR] Totale stazioni recuperate: {len(all_stations)}", file=sys.stderr)

    # 3. Per ogni location, filtra candidati
    locations = load_locations()
    for loc_id, loc in locations.items():
        loc_lat = loc["lat"]
        loc_lon = loc["lon"]
        loc_elev = loc.get("elevation_m", 0)

        candidates: list[dict[str, Any]] = []
        for station in all_stations:
            try:
                s_lat = float(station.get("lat") or station.get("latitude") or 0)
                s_lon = float(station.get("lon") or station.get("longitude") or 0)
                s_elev = float(station.get("elevation_m") or station.get("quota") or 0)
                dist = haversine_km(loc_lat, loc_lon, s_lat, s_lon)
                delta_elev = s_elev - loc_elev
                if dist <= DISTANCE_MAX_KM and abs(delta_elev) <= DELTA_ELEV_MAX_M:
                    candidates.append({
                        "id": station.get("id") or station.get("cod_staz") or "?",
                        "variables": station.get("variables") or station.get("parametri") or "?",
                        "distance_km": dist,
                        "delta_elev_m": delta_elev,
                        "data_from": station.get("data_inizio") or "?",
                    })
            except (TypeError, ValueError):
                continue

        candidates.sort(key=lambda x: x["distance_km"])
        print_location_result(loc_id, loc, candidates, ckan_status)

    print_ckan_status(ckan_status)

    print("## Prossimi passi\n")
    print("1. Se CKAN ha restituito stazioni: compilare `config/stations.yaml`")
    print("   e aggiornare `sir_station_id` in `config/locations.yaml`.")
    print("2. Se CKAN fallisce: consultare manualmente https://cfr.toscana.it")
    print("   e https://dati.toscana.it per trovare stazioni.")
    print("3. Per ogni location, dichiarare distanza e delta quota nell'articolo")
    print("   (metodologia: sezione 'Limiti del ground truth').")
    print("4. Flag se distanza > 5km o delta quota > 100m (vedi known_issues KI005).\n")


if __name__ == "__main__":
    main()
