"""Fetcher real-time SIR Toscana.

Endpoint scoperto via HAR (2026-05-13) — non documentato ufficialmente:
  - Real-time: GET actions.php?action=station&id={station_id}
  - Metadata:  GET ajax_stations.php?id={station_id}&types=undefined

Entrambi richiedono header X-Requested-With: XMLHttpRequest e Referer.
Senza Referer actions.php risponde con pagina HTML invece di JSON.
Nessuna autenticazione necessaria.

Rate limit empirico: ~1 req/s prima di ricevere HTTP 429.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

BASE_URL = "https://www.sir.toscana.it/open_layers"
_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.sir.toscana.it/",
}
_INTER_REQUEST_DELAY = 1.0  # secondi tra richieste consecutive (evita 429)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def fetch_station_realtime(station_id: str) -> dict[str, Any]:
    """Recupera letture real-time per una stazione SIR.

    Returns:
        Dizionario con i dati restituiti dall'API (struttura variabile per tipo stazione).
        Esempio chiavi: 'termo', 'igro', 'anemo', 'pluvio', ciascuno con
        valore corrente e cumulati CUM00–CUM36.

    Raises:
        httpx.HTTPStatusError: se la risposta non è 2xx dopo i retry.
    """
    url = f"{BASE_URL}/actions.php"
    params = {"action": "station", "id": station_id}
    logger.debug(f"SIR realtime: {station_id}")
    with httpx.Client(timeout=15) as client:
        r = client.get(url, params=params, headers=_HEADERS)
        r.raise_for_status()
    return r.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def fetch_station_meta(station_id: str) -> dict[str, Any]:
    """Recupera metadata di una stazione SIR (nome, quota, sensori disponibili).

    Returns:
        Dizionario con i metadata restituiti dall'API.
    """
    url = f"{BASE_URL}/ajax_stations.php"
    params = {"id": station_id, "types": "undefined"}
    logger.debug(f"SIR meta: {station_id}")
    with httpx.Client(timeout=15) as client:
        r = client.get(url, params=params, headers=_HEADERS)
        r.raise_for_status()
    return r.json()


def fetch_stations_realtime(
    station_ids: list[str],
    delay: float = _INTER_REQUEST_DELAY,
) -> dict[str, dict[str, Any]]:
    """Recupera real-time per una lista di stazioni con throttling.

    Args:
        station_ids: Lista di ID stazione SIR (es. ['TOS01001225', 'TOS01001215']).
        delay: Pausa in secondi tra una richiesta e l'altra (default 1.0s).

    Returns:
        Dict {station_id: dati} — le stazioni con errore vengono omesse.
    """
    results: dict[str, dict[str, Any]] = {}
    for i, sid in enumerate(station_ids):
        if i > 0:
            time.sleep(delay)
        try:
            results[sid] = fetch_station_realtime(sid)
        except Exception as e:
            logger.warning(f"SIR realtime fallito per {sid}: {e}")
    return results
