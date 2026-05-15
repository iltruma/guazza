# Guazza — Stato corrente

> Aggiornato: 2026-05-15

## Cosa è stato fatto

### Sprint 0 — Ricognizione (completato)
- Identificate 22 stazioni SIR, 6 stazioni ARPAT
- `config/stations.yaml` completo con coordinate, sensori verificati via API, `used_by` per location
- `config/sources.yaml`, `config/locations.yaml`, `config/indicators.yaml` presenti
- Script `scripts/01–04_*.py` per ricognizione sorgenti

### Refactoring repo — schema wide + struttura flat (completato — 2026-05-15)
- **Schema DuckDB wide**: una riga per `(source, station_id, ts)` con colonne `temp_c`, `humidity_pct`, `precip_mm`, `wind_speed_ms`, ...
  - `observations`, `forecasts`, `predictions`, `benchmark_forecasts` tutte wide
  - Eliminate `hydro_observations` e `air_quality` (assorbite in `observations` con colonne sparse)
- **Eliminato sistema migrations**: DuckDB file-based è ricostruibile; `schema.sql` unico source of truth
- **Struttura flat**:
  - `src/guazza/fetchers.py` — SIR storico + realtime + Netatmo (ex `ingestion/`)
  - `src/guazza/storage.py` — DuckDB client (ex `storage/duckdb_client.py`)
  - `src/guazza/weights.py` — pesi stazioni (ex `storage/station_weights.py`)
  - `src/guazza/indicators.py` — DLE (ex `indicators/engine.py`)
  - `src/guazza/jobs/ingest.py` — entry point cron unificato
  - Eliminati 6 package vuoti (`evaluation/`, `features/`, `models/`, `output/`, ecc.)
- **Ingestion wide**:
  - SIR storico (`fetch_sir_historical`): output dict wide, una riga per giorno (non EAV)
  - SIR realtime (`fetch_sir_realtime`): fix header `Referer`, output wide
  - Netatmo (`fetch_netatmo_location` + `save_netatmo_to_db`): una riga per stazione in `observations`
- **Test**: 67 pass, ruff OK, mypy OK

### Note tecniche

#### IDST CSV SIR (endpoint download.php)
| Sensore | IDST |
|---|---|
| termometro | `termo_csv` |
| pluviometro | `pluvio0_24` |
| igrometro | `igro0_24` |
| anemometro | `anemo0_24` |
| idrometro | `idro_l` |

barometro, radiometro_*, evaporimetro: **solo realtime**, nessuno storico CSV.

#### Realtime SIR (endpoint actions.php)
Richiede header `X-Requested-With: XMLHttpRequest` **e** `Referer: https://www.sir.toscana.it/`.
Senza Referer risponde con pagina HTML (redirect al portale).

### Smoke test multi-sensore SIR + fix bug storage (completato — 2026-05-15)
- 6 test per `upsert_sir_observations`: merge 4 sensori → 1 riga wide, COALESCE, idempotenza
- Fix bug: `hum_med_pct=0.0` era trattato come falsy con `or` → sostituito con `is not None`
- Rimossi duplicati di fixture in `test_storage.py`

### Ingestion Open-Meteo (completato — 2026-05-15)
- `fetch_openmeteo_forecast`: fetch live multi-modello (ecmwf_ifs025, icon_eu, gfs025, arome_france)
- `fetch_openmeteo_historical`: backfill storico (Historical Forecast API, 2022+)
- `fetch_openmeteo_all_locations`: wrapper per tutte le 4 location
- `ts_run` inferita per difetto all'ultimo run nominale del modello (ECMWF: 0/12 UTC, ICON-EU: ogni 3h, GFS: ogni 6h)
- `upsert_forecasts` in `DuckDBClient`: UPSERT su tabella `forecasts` wide, DO UPDATE sovrascrive (l'ultimo run vince)
- 16 test nuovi (mock HTTP, parse, lead_time_h, upsert idempotenza/conflict)

## Prossimi passi (in ordine)

1. **Job end-to-end**: `jobs/ingest.py` deve orchestrare SIR + Netatmo + Open-Meteo per tutte le location
2. **Feature engineering (Sprint 2)**: lag temporali + join `observations` ↔ `forecasts` wide per training set LightGBM
