"""Fetcher storico SIR Toscana — endpoint CSV diretto.

Endpoint (scoperto 2026-05-14, non documentato ufficialmente):
    GET https://www.sir.toscana.it/archivio/download.php?IDST={sensor_type}&IDS={station_id}

Restituisce tutto lo storico disponibile per la stazione in un CSV semicolon-separated,
decimale virgola (italiano). Nessun parametro anno: un'unica chiamata per tutto lo storico.

Struttura CSV:
    - Righe 1–20: metadati stazione + legenda flag (skippate)
    - Riga header: '"gg/mm/aaaa";"col1";"col2"...'
    - Righe dati: 'gg/mm/aaaa;valore1;valore2...'

Tipi sensore (IDST) supportati e colonne attese:
    termo_csv:   [tmax_c, tmin_c]                        — no colonna flag
    pluvio0_24:  [precip_mm] + "Tipo Dato"               — colonna flag separata
    igro0_24:    [hum_med_pct, hum_min_pct, hum_max_pct] — no colonna flag
    anemo0_24:   [wind_speed_ms, wind_dir_deg, wind_gust_ms] — no colonna flag
    idro_l:      [level_m] + "Tipo Dato"                 — colonna flag separata

Flag "Tipo Dato" (pluvio e idro):
    V → 'ok'          (validato)
    N → 'ok'          (non validato — usabile, soggetto a revisione)
    P → 'ok'          (prevalidato)
    R → 'reconstructed'
    I → 'uncertain'
    @ → 'missing'

Cella vuota → value=None, flag='missing' (per tutti i tipi).

Output: lista di dict compatibili con INSERT nella tabella `observations` DuckDB.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

BASE_URL = "https://www.sir.toscana.it/archivio/download.php"
_HEADERS = {"X-Requested-With": "XMLHttpRequest"}
_INTER_REQUEST_DELAY = 1.2  # secondi tra richieste (evita 429)

# Direzioni vento abbreviate → gradi (16 punti cardinali)
_WIND_DIR_DEG: dict[str, float] = {
    "N":   0.0,
    "NNE": 22.5,
    "NE":  45.0,
    "ENE": 67.5,
    "E":   90.0,
    "ESE": 112.5,
    "SE":  135.0,
    "SSE": 157.5,
    "S":   180.0,
    "SSO": 202.5,
    "SO":  225.0,
    "OSO": 247.5,
    "O":   270.0,
    "ONO": 292.5,
    "NO":  315.0,
    "NNO": 337.5,
}

# Flag SIR → flag interno Guazza
_FLAG_MAP: dict[str, str] = {
    "V": "ok",           # validato
    "N": "ok",           # non validato (soggetto a revisione, ma usabile)
    "P": "ok",           # prevalidato
    "R": "reconstructed",
    "I": "uncertain",
    "@": "missing",
}

# Schema per tipo sensore:
#   variables: nomi variabili nell'ordine delle colonne CSV (esclusa data e flag)
#   flag_col:  True se esiste una colonna "Tipo Dato" alla fine della riga
_SENSOR_SCHEMA: dict[str, dict[str, Any]] = {
    "termo_csv": {
        "variables": ["tmax_c", "tmin_c"],
        "flag_col": False,
    },
    "pluvio0_24": {
        "variables": ["precip_mm"],
        "flag_col": True,
    },
    "igro0_24": {
        "variables": ["hum_med_pct", "hum_min_pct", "hum_max_pct"],
        "flag_col": False,
    },
    "anemo0_24": {
        "variables": ["wind_speed_ms", "wind_dir_deg", "wind_gust_ms"],
        "flag_col": False,
    },
    "idro_l": {
        "variables": ["level_m"],
        "flag_col": True,
    },
}


def _parse_value(raw: str, var_name: str) -> float | None:
    """Converte una stringa CSV in float. Gestisce decimale virgola e direzione vento."""
    s = raw.strip()
    if not s:
        return None
    if var_name == "wind_dir_deg":
        deg = _WIND_DIR_DEG.get(s.upper())
        if deg is None:
            logger.debug(f"Direzione vento sconosciuta: {s!r}")
        return deg
    try:
        return float(s.replace(",", "."))
    except ValueError:
        logger.debug(f"Valore non parsabile: {s!r} per variabile {var_name!r}")
        return None


def fetch_station_csv(
    station_id: str,
    sensor_type: str,
    location_id: str = "",
) -> list[dict[str, Any]]:
    """Scarica e parsa tutto lo storico CSV per una stazione SIR.

    Args:
        station_id:   ID stazione SIR (es. 'TOS01001215').
        sensor_type:  Tipo sensore (IDST): 'termo_csv' | 'pluvio0_24' | 'igro0_24'
                      | 'anemo0_24' | 'idro_l'.
        location_id:  ID location Guazza da associare (es. 'lavoro_cosimo').

    Returns:
        Lista di dict con chiavi:
            source, station_id, location_id, ts, variable, value, flag
        Compatibile con INSERT nella tabella `observations` DuckDB.
        Le righe con value=None e flag='missing' sono incluse (QC downstream).

    Raises:
        ValueError: se sensor_type non è tra quelli supportati.
        httpx.HTTPStatusError: se la risposta non è 2xx dopo i retry.
    """
    schema = _SENSOR_SCHEMA.get(sensor_type)
    if schema is None:
        raise ValueError(
            f"sensor_type {sensor_type!r} non supportato. "
            f"Valori validi: {list(_SENSOR_SCHEMA)}"
        )
    return _fetch_csv_with_retry(station_id, sensor_type, location_id, schema)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
def _fetch_csv_with_retry(
    station_id: str,
    sensor_type: str,
    location_id: str,
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    logger.debug(f"SIR CSV fetch: {station_id} {sensor_type}")

    with httpx.Client(timeout=30) as client:
        r = client.get(
            BASE_URL,
            params={"IDST": sensor_type, "IDS": station_id},
            headers=_HEADERS,
        )
        r.raise_for_status()

    variables: list[str] = schema["variables"]  # type: ignore[assignment]
    has_flag_col: bool = schema["flag_col"]  # type: ignore[assignment]

    # Trova la riga header ("gg/mm/aaaa") e salta tutto sopra
    lines = r.text.splitlines()
    data_start = 0
    for i, line in enumerate(lines):
        if "gg/mm/aaaa" in line:
            data_start = i + 1
            break

    if data_start == 0:
        logger.warning(f"Header 'gg/mm/aaaa' non trovato: {station_id} {sensor_type}")
        return []

    records: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO("\n".join(lines[data_start:])), delimiter=";")

    for row in reader:
        if not row or not row[0].strip():
            continue

        date_str = row[0].strip()
        try:
            ts = datetime.strptime(date_str, "%d/%m/%Y")
        except ValueError:
            logger.debug(f"Data non parsabile: {date_str!r}")
            continue

        # Estrai flag dalla colonna finale (se presente)
        if has_flag_col and len(row) >= len(variables) + 2:
            raw_flag = row[len(variables) + 1].strip()
            row_flag = _FLAG_MAP.get(raw_flag, "ok")
        else:
            row_flag = None  # determinato per cella sotto

        # Parsa ogni variabile
        for i, var in enumerate(variables):
            col_idx = i + 1  # colonna 0 = data
            raw = row[col_idx].strip() if col_idx < len(row) else ""

            value = _parse_value(raw, var)

            if has_flag_col:
                flag = row_flag if value is not None else "missing"
            else:
                # Per i tipi senza colonna flag, cella vuota = missing, altrimenti ok
                flag = "ok" if value is not None else "missing"

            records.append({
                "source":      "sir_toscana",
                "station_id":  station_id,
                "location_id": location_id,
                "ts":          ts,
                "variable":    var,
                "value":       value,
                "flag":        flag,
            })

    logger.info(
        f"SIR CSV: {station_id} {sensor_type} → {len(records)} record"
    )
    return records
