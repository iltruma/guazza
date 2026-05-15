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

## Prossimi passi (in ordine)

1. **Smoke test reale SIR: PASSATO** — scaricate 12.552 righe (34 anni, 1992–2026) per TOS01001215, inserite in DuckDB wide, PK source+station_id+ts mantiene 1 riga/giorno correttamente
2. **Ingestion Open-Meteo**: implementare fetch forecast NWP multi-modello → tabella `forecasts` wide
3. **Job end-to-end**: `jobs/ingest.py` deve orchestrare fetch SIR + Netatmo + Open-Meteo per tutte le location
5. **Smoke test multi-sensore SIR**: per TOS01001215, scaricare pluvio + anemo + igro, verificare UPSERT wide corretto (una riga per giorno con più colonne)
6. **Ingestion Open-Meteo**: implementare fetch forecast NWP multi-modello → tabella `forecasts` wide
7. **Job end-to-end**: `jobs/ingest.py` deve orchestrare fetch SIR + Netatmo + Open-Meteo per tutte le location
8. **Feature engineering (Sprint 2)**: lag temporali + join `observations` ↔ `forecasts` wide per training set LightGBM
