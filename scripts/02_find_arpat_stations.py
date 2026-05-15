#!/usr/bin/env python3
"""Task 0 — Script 02: trova stazioni ARPAT qualità aria per le 4 location.

Interroga il portale ARPAT Toscana per trovare la stazione di monitoraggio
qualità aria più vicina a ogni location, e identifica la zona omogenea
di appartenenza per i bollettini di qualità aria.

Output: report Markdown su stdout (redirigere su docs/task0_arpat_stations.md).

Uso:
    uv run python scripts/02_find_arpat_stations.py > docs/task0_arpat_stations.md
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from bs4 import BeautifulSoup

LOCATIONS_YAML = Path(__file__).parent.parent / "config" / "locations.yaml"

# Possibili endpoint ARPAT — da verificare (vedi sources.yaml)
ARPAT_REALTIME_URL = (
    "https://www.arpat.toscana.it/dati-e-indicatori/dati-in-tempo-reale/qualita-dell-aria"
)
# Alcuni portali ARPAT espongono JSON via endpoint dedicato
ARPAT_JSON_CANDIDATES = [
    "https://www.arpat.toscana.it/dati-e-indicatori/open-data/qualita-aria",
    "https://www.arpat.toscana.it/dati-e-indicatori/dati-in-tempo-reale/qualita-dell-aria/dati",
]

# Zone omogenee ARPAT Toscana (ufficiali, da bollettino)
ARPAT_ZONES = {
    "agglomerato_firenze": "Agglomerato di Firenze (FI)",
    "zona_prato_pistoia": "Zona Prato-Pistoia (PO/PT)",
    "zona_valdarno_aretino_valdichiana": "Zona Valdarno Aretino-Valdichiana (AR)",
    "zona_costiera": "Zona Costiera",
    "zona_collinare_montana": "Zona Collinare-Montana",
}

DISTANCE_MAX_KM = 15.0   # qualità aria: raggio più ampio rispetto a stazioni meteo


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_locations() -> dict[str, Any]:
    with open(LOCATIONS_YAML) as f:
        return yaml.safe_load(f)["locations"]


def probe_arpat_json() -> dict[str, Any]:
    """Prova gli endpoint JSON candidati di ARPAT."""
    results: dict[str, Any] = {}
    for url in ARPAT_JSON_CANDIDATES:
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True)
            content_type = r.headers.get("content-type", "")
            results[url] = {
                "status_code": r.status_code,
                "content_type": content_type,
                "is_json": "json" in content_type,
                "size_bytes": len(r.content),
            }
            if "json" in content_type:
                try:
                    results[url]["sample"] = r.json()
                except Exception:
                    results[url]["sample"] = None
        except Exception as e:
            results[url] = {"error": str(e)}
    return results


def scrape_arpat_stations_html() -> list[dict[str, Any]]:
    """Fallback: prova a estrarre stazioni dalla pagina HTML ARPAT."""
    stations: list[dict[str, Any]] = []
    try:
        r = httpx.get(ARPAT_REALTIME_URL, timeout=20, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        # Cerca tabelle o elementi con dati stazione — selettori da aggiornare
        # dopo aver ispezionato la pagina manualmente
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows[1:]:   # skip header
                cells = row.find_all(["td", "th"])
                if len(cells) >= 3:
                    stations.append({
                        "raw_cells": [c.get_text(strip=True) for c in cells],
                        "source": "html_scraping",
                    })
    except Exception as e:
        print(f"  [ARPAT] Errore scraping HTML: {e}", file=sys.stderr)

    return stations


def print_report_header() -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("# Task 0 — Stazioni ARPAT qualità aria per le 4 location\n")
    print(f"Generato: {now}\n")
    print("Criteri: stazione più vicina entro 15 km + zona omogenea di appartenenza.\n")
    print("---\n")


def print_endpoint_probe(probe_results: dict[str, Any]) -> None:
    print("## Probe endpoint ARPAT\n")
    for url, result in probe_results.items():
        status = result.get("status_code", "ERR")
        ct = result.get("content_type", "?")
        is_json = result.get("is_json", False)
        err = result.get("error", "")
        icon = "✅" if is_json else ("⚠️" if not err else "❌")
        print(f"### {icon} `{url}`")
        if err:
            print(f"- **Errore**: `{err}`")
        else:
            print(f"- Status: `{status}`")
            print(f"- Content-Type: `{ct}`")
            print(f"- JSON disponibile: {'**SÌ**' if is_json else 'NO'}")
            if result.get("sample"):
                keys = list(result["sample"].keys()) if isinstance(result["sample"], dict) else "array"
                print(f"- Chiavi JSON: `{keys}`")
        print()

    print("---\n")


def print_location_result(
    loc_id: str,
    loc: dict[str, Any],
    arpat_zone: str,
    stations_found: list[dict[str, Any]],
) -> None:
    print(f"## {loc_id} — {loc['label']}")
    print(f"- Indirizzo: {loc['address']}")
    print(f"- Coordinate (stimate): ({loc['lat']}, {loc['lon']})")
    print(f"- Zona ARPAT (da config): `{arpat_zone}` — {ARPAT_ZONES.get(arpat_zone, '?')}\n")

    if not stations_found:
        print("**Nessuna stazione trovata automaticamente.**\n")
        print("**Azione richiesta**: trovare manualmente la stazione ARPAT su")
        print("https://www.arpat.toscana.it/dati-e-indicatori/dati-in-tempo-reale/qualita-dell-aria")
        print(f"e aggiornare `arpat_station_id` in `config/locations.yaml` per `{loc_id}`.\n")
    else:
        print("| Stazione | Comune | Distanza (km) | Variabili | Note |")
        print("|---|---|---|---|---|")
        for s in stations_found[:3]:
            print(
                f"| {s.get('id', '?')} | {s.get('comune', '?')} | "
                f"{s.get('distance_km', '?'):.1f} | {s.get('variables', '?')} | |"
            )
        print()

    print(f"> Note: {loc.get('notes', '—')}\n")
    print("---\n")


def main() -> None:
    print_report_header()

    # 1. Proba endpoint JSON ARPAT
    print("  [ARPAT] Probe endpoint JSON...", file=sys.stderr)
    probe_results = probe_arpat_json()
    print_endpoint_probe(probe_results)

    # 2. Se nessun JSON, prova scraping HTML
    has_json = any(r.get("is_json") for r in probe_results.values())
    if not has_json:
        print("  [ARPAT] Nessun JSON endpoint trovato, provo scraping HTML...", file=sys.stderr)
        stations_raw = scrape_arpat_stations_html()
        print(f"  [ARPAT] Righe HTML estratte: {len(stations_raw)}", file=sys.stderr)
    else:
        stations_raw = []

    # 3. Per ogni location
    locations = load_locations()
    print("## Stazioni per location\n")
    for loc_id, loc in locations.items():
        arpat_zone = loc.get("arpat_zone", "?")
        # Nota: senza stazioni parsed strutturate, questa lista è vuota
        # L'utente dovrà compilare manualmente dopo aver esplorato il portale ARPAT
        candidates: list[dict[str, Any]] = []
        print_location_result(loc_id, loc, arpat_zone, candidates)

    print("## Zone omogenee ARPAT Toscana\n")
    print("Mappatura ufficiale zone ARPAT (per bollettini qualità aria):\n")
    for key, label in ARPAT_ZONES.items():
        print(f"- `{key}`: {label}")
    print()

    print("## Prossimi passi\n")
    print("1. Se endpoint JSON trovato: parsare risposta e aggiornare `arpat_station_id`")
    print("   in `config/locations.yaml` per ogni location.")
    print("2. Se solo HTML: consultare manualmente il portale ARPAT, trovare stazione")
    print("   più vicina (entro 15 km) per ogni location.")
    print("3. Verificare che la zona omogenea in `config/locations.yaml` sia corretta.")
    print("4. Aggiornare `config/stations.yaml` con i dettagli delle stazioni ARPAT scelte.\n")


if __name__ == "__main__":
    main()
