# Guazza — Stato corrente

> Aggiornato: 2026-05-14

## Cosa è stato fatto

### Sprint 0 — Ricognizione (completato)
- Identificate 22 stazioni SIR, 6 stazioni ARPAT
- `config/stations.yaml` completo con coordinate, sensori verificati via API, `used_by` per location
- `config/sources.yaml`, `config/locations.yaml`, `config/indicators.yaml` presenti
- Script `scripts/01–04_*.py` per ricognizione sorgenti

### Storage (completato)
- `src/guazza/storage/duckdb_client.py` — DuckDB client con lock file
- `src/guazza/storage/schema.sql` — schema completo (10 tabelle)
- `src/guazza/storage/migrations.py` — 4 migrations (v1–v4)
- `src/guazza/storage/station_weights.py` — pesi distanza/quota
- Test: 100% pass (100 test totali)

### Ingestion SIR storico (completato — 2026-05-14)
- Riscritto `src/guazza/ingestion/sir_historical.py` da parser HTML a CSV diretto
- Endpoint scoperto: `GET /archivio/download.php?IDST=<idst>&IDS=<station_id>`
  - Restituisce tutto lo storico in un colpo, nessun parametro anno
  - Separatore `;`, decimale `,`
- Bug risolti rispetto alla versione HTML:
  - IDST corretto per temperatura: `termo_csv` (non `termo`)
  - Dizionario direzioni vento: abbreviazioni reali (N/NE/E/SE/S/SO/O/NO)
  - Ordine colonne `anemo0_24`: `Vel Med; Dir Med; Vel Max` → `wind_speed_ms, wind_dir_deg, wind_gust_ms`
  - Flag inline per pluvio/idro via colonna "Tipo Dato" (V/N/P → ok, R → reconstructed, I → uncertain, @ → missing)
- `config/stations.yaml` aggiornato con `sir_idst_map` (5 IDST noti)
- 14 test unitari offline (mock httpx), tutti passano

### Ingestion SIR realtime (parzialmente completato)
- `src/guazza/ingestion/sir_realtime.py` presente
- **BUG NOTO**: `actions.php` restituisce HTML invece di JSON senza header `Referer`
  - Fix: aggiungere `"Referer": "https://www.sir.toscana.it/"` agli headers
  - `ajax_stations.php` funziona già con Referer (verificato)
- Nessun test ancora

### Altri moduli presenti ma non testati end-to-end
- `src/guazza/ingestion/netatmo_realtime.py` — dinamico, con test
- `src/guazza/indicators/engine.py` — DLE, con test
- `src/guazza/jobs/` — entry point cron (skeleton)
- `frontend-v1/` — HTML/JS statico (skeleton)

## Prossimi passi (in ordine)

1. **Fix `sir_realtime.py`**: aggiungere `Referer` header, verificare struttura JSON risposta, scrivere test
2. **Job ingestion SIR**: implementare `jobs/ingest_realtime.py` per SIR (carica storico CSV al primo run, poi solo realtime)
3. **Integrazione DuckDB**: collegare `fetch_station_csv` → `duckdb_client.bulk_insert` nella tabella `observations`
4. **Smoke test reale**: scaricare un anno di dati per TOS01001215, verificare record in DuckDB
5. **Ingestion Open-Meteo**: forecast NWP multi-modello

## Note tecniche

### IDST CSV SIR (endpoint download.php)
| Sensore | IDST |
|---|---|
| termometro | `termo_csv` |
| pluviometro | `pluvio0_24` |
| igrometro | `igro0_24` |
| anemometro | `anemo0_24` |
| idrometro | `idro_l` |

barometro, radiometro_*, evaporimetro: **solo realtime**, nessuno storico CSV.

### Realtime SIR (endpoint actions.php)
Richiede header `X-Requested-With: XMLHttpRequest` **e** `Referer: https://www.sir.toscana.it/`.
Senza Referer risponde con pagina HTML (redirect al portale).
