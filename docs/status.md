# Guazza — Stato corrente

> Aggiornato: 2026-05-15

## Cosa è stato fatto

### Sprint 0 — Ricognizione (completato)
- Identificate 22 stazioni SIR, 6 stazioni ARPAT
- `config/stations.yaml` completo con coordinate, sensori verificati via API, `used_by` per location
- `config/sources.yaml`, `config/locations.yaml`, `config/indicators.yaml` presenti

### Refactoring repo — schema wide + struttura flat (completato — 2026-05-15)
- **Schema DuckDB wide**: una riga per `(source, station_id, ts, granularity)`
  - `observations`, `forecasts`, `predictions`, `benchmark_forecasts` tutte wide
  - `granularity VARCHAR NOT NULL` in PK: distingue `daily` (CSV SIR) da `realtime` (actions.php, Netatmo)
  - `precip_interval_h TINYINT`: disambigua cumulato 24h (SIR storico) da misura 1h (realtime/Netatmo)
- **Eliminato sistema migrations**: `schema.sql` unico source of truth
- **Struttura flat**:
  - `src/guazza/fetchers.py` — SIR storico + realtime + Netatmo + Open-Meteo
  - `src/guazza/storage.py` — DuckDB client con lock file, `upsert_sir_observations` (batch), `upsert_forecasts`
  - `src/guazza/weights.py` — pesi stazioni (decay distanza + quota)
  - `src/guazza/indicators.py` — DLE (Decision Logic Engine)
  - `src/guazza/jobs/ingest.py` — 4 comandi cron (historical/daily/realtime/forecasts)

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
Il campo `date` nel JSON contiene il timestamp della misura (formato `DD/MM/YYYY HH:MM`).

#### granularity in PK observations
`PRIMARY KEY (source, station_id, ts, granularity)` — necessario perché SIR storico scrive
sempre `ts=00:00:00` (aggregato giornaliero); un realtime chiamato a mezzanotte produrrebbe
la stessa PK. Valori: `daily`, `realtime`, `hourly` (riservato).

### Bug fix da revisione (completato — 2026-05-15)
- **B1** `weights.py`: `quota_m=0` non più trattato come falsy (`is not None`)
- **B2** `fetchers.py`: SIR realtime ts parsato da campo `date` nel JSON, fallback `now(UTC)`
- **B3** `storage.py`: `upsert_sir_observations` → `executemany` batch (era loop insert singolo)
- **B4** `storage.py`: `init_schema` esegue script intero (era `split(";")` fragile)
- **B5** `fetchers.py`: `INTERVAL` via f-string (era concatenazione stringa+parametro)
- **B8** `fetchers.py`: `_log_scrape()` — log JSON strutturato su tutti i fetcher
- **B11** `schema.sql` + storage + fetchers: `precip_interval_h` + `granularity` in PK

### Ingestion Open-Meteo (completato — 2026-05-15)
- `fetch_openmeteo_forecast`: fetch live multi-modello (ecmwf_ifs025, icon_eu, gfs025, arome_france)
- `fetch_openmeteo_historical`: backfill storico (Historical Forecast API, 2022+)
- `ts_run` inferita per difetto all'ultimo run nominale del modello (ECMWF: 0/12 UTC, ICON-EU: ogni 3h, GFS: ogni 6h)
- `upsert_forecasts`: UPSERT batch, l'ultimo run vince

### Job di ingestion (completato — 2026-05-15)
Quattro comandi in `src/guazza/jobs/ingest.py`:

| Comando | Schedulazione | Cosa fa |
|---|---|---|
| `historical` | one-shot manuale | Backfill SIR CSV + Open-Meteo 2022→oggi |
| `daily` | cron 06:00 UTC | Delta di ieri: SIR CSV + Open-Meteo historical |
| `realtime` | cron ogni 15-30 min | SIR actions.php + Netatmo tutte le location |
| `forecasts` | cron ogni 6h | Open-Meteo forecast multi-modello, 7 giorni |

Ogni job: `--dry-run`, Healthchecks.io ping (start/ok/fail), log JSON strutturato stdout, exit 1 su eccezione.

### Logging (completato — 2026-05-15)
- Loguru configurato con `serialize=True` su stdout nei comandi CLI (`_setup_logging()`)
- Non attivato a livello di modulo — i test pytest non sono impattati
- In produzione: aggiungere `logger.add(file, serialize=True, rotation="1 day")` in `_setup_logging()`

### Pulizia repo (completato — 2026-05-15)
- Rimossi `scripts/` (5 file Sprint 0) e `notebooks/` (gitkeep)
- Aggiornati `README.md`, `AGENTS.md`, `config/sources.yaml` per rimuovere ogni riferimento

## Test
- **118 test**, tutti verdi
- `ruff check` OK, `mypy` OK

## Prossimi passi (in ordine)

### Sprint 1b — ARPAT qualità aria (completato — 2026-05-15)
- `fetch_arpat_nrt()`: valori orari NO2/O3 da endpoint NRT ARPAT, `granularity='hourly'`
- `fetch_arpat_bollettini()`: PM10/PM2.5 giornalieri da endpoint bollettini, `granularity='daily'`
- `fetch_arpat_all_locations()`: wrapper per tutte le location con `arpat_stations` in config
- Integrato in job `realtime` (NRT) e `daily` (bollettini)
- Parsing difensivo: supporta formato lista e dict `{"stazioni": [...]}`
- 11 test pytest, tutti verdi
- CFR Toscana rimosso da `sources.yaml` (coperto da SIR idrometria)
- RainViewer marcato `scope: frontend_only` (nessun fetcher, usato solo in Sprint 6)

🟡 **Punto aperto**: struttura JSON reale degli endpoint ARPAT non verificata su rete reale.
Il parsing è difensivo (supporta più formati), ma da verificare al primo run su VPS.

### Sprint 2b — Backfill SIR pre-2022
**Dipendenza**: ingestion funzionante (Sprint 1 completato)

- Estendere `fetch_sir_historical` per scaricare CSV SIR oltre il 2022 (endpoint download.php supporta date precedenti)
- Verificare limite effettivo disponibilità archivio SIR per ogni stazione
- Caricare in DuckDB con stessa logica upsert esistente

### Sprint 2c — Check qualitativo dati SIR
**Dipendenza**: backfill pre-2022 completato (Sprint 2b)

- **Copertura temporale**: % timestamp attesi con dato presente, per stazione × sensore × anno/mese
- **Outlier fisici**: valori fuori range plausibile (temp < -20°C o > 50°C, umidità > 100%, vento < 0, ecc.) — flaggare in DuckDB, non eliminare
- **Spike detection**: ΔT > 10°C in 1h o equivalente per altri sensori — flaggare
- **Correlazione inter-stazione**: per ogni location, correlazione tra stazioni vicine; bassa correlazione segnala stazione problematica
- **Report per location**: copertura ottimale per periodo, gap rilevanti evidenziati
- Output: tabella `quality_flags` in DuckDB + report Markdown statico
- Decisioni di esclusione stazioni/periodi: manuali, non automatiche

### Sprint 3 — Feature Engineering
**Dipendenza**: dati in DuckDB validati (`observations` + `forecasts`)

- Join `observations ↔ forecasts` per ogni `(location_id, ts, lead_time_h)`
- Feature NWP: temperatura prevista, umidità, vento, precipitazioni × 4 modelli × lead time
- Feature osservativa: valori SIR pesati per location (`weights.py`)
- Feature climatologiche statiche: media/std mensile SIR multi-anno (mai ERA5 dinamico — D-001)
- `location_id` categorica (D-005), `lead_time_h` come feature numerica
- Output: training set materializzato in Parquet o DuckDB view

🟡 **Punto aperto**: copertura storica SIR per le 4 location da verificare in Sprint 2c
(minimo ~200 esempi per bucket lead time per CQR stabile — D-003)

### Sprint 4 — Modello ML
**Dipendenza**: training set Sprint 3

- LightGBM quantile regression (α = 0.05, 0.10, 0.50, 0.90, 0.95)
- Cross-validation temporale walk-forward con embargo 7 giorni (D-002)
- CQR calibration stratificata per 5 bucket lead time: `0-6h`, `6-12h`, `12-24h`, `24-48h`, `48-72h` (D-003)
- Metriche: CRPS, coverage empirica, skill score vs NWP grezzo come benchmark
- **Benchmark formale in produzione**: popolare tabella `benchmark_forecasts` in DuckDB con NWP grezzo (Open-Meteo senza post-processing) per confronto sistematico nel tempo
- Persistenza modello + artefatti calibrazione su disco

### Sprint 5 — Output JSON + Decision Logic Engine
**Dipendenza**: modello calibrato Sprint 4

- JSON writer per ogni location: punto mediano + CI80 + CI90 + `coverage_empirical_30d` (D-004)
- `coverage_empirical_30d`: rolling window 30 giorni osservazioni vs CI; se dati insufficienti → `null`
- Decision Logic Engine: regole su distribuzione probabilistica → indicatori operativi
  (`panni`, `motorino`, `gelata`, ecc.)
- Logging obbligatorio in `indicator_log` DuckDB per ogni invocazione DLE (D-009)
- Job cron `predict` che chiama modello → JSON → DLE → `indicator_log`

### Sprint 6 — Frontend
**Dipendenza**: JSON output Sprint 5 stabile

- HTML + JS vanilla, zero dipendenze JS
- Una pagina per location: indicatori operativi prominenti + CI meteo
- Badge `coverage_empirical_30d` visibile ("calibrazione in corso" se null)
- Nginx statico, Cloudflare CDN/WAF
- Nessun framework, nessun bundler

### Sprint 7 — Deploy VPS
**Dipendenza**: tutto funzionante e testato in locale

- Provisioning Hetzner CX22, Ubuntu 24.04 LTS
- Backfill storico (`historical`) per caricare SIR + Open-Meteo 2022→oggi
- Crontab con i 4 job ingestion + job `predict`
- Configurazione `.env` produzione, Healthchecks.io, UptimeRobot
- **Backup Cloudflare R2**: job cron periodico per backup `.duckdb` + Parquet su Cloudflare R2 (10GB free tier, egress gratis) via `rclone` o `boto3`
- GitHub Actions → deploy SSH

### Sprint 8 — Model monitoring
**Dipendenza**: Deploy VPS completato (Sprint 7)

- Job cron che calcola `coverage_empirical_30d` rolling e la confronta con target (80% per CI80, 90% per CI90)
- Alert se coverage scende sotto soglia: log `ERROR` + ping `Healthchecks.io` fail
- Requisito obbligatorio D-004

### Sprint 9 — Calibrazione soglie DLE post-deploy
**Dipendenza**: 30-60 giorni di operatività in produzione (Sprint 7+8)

- Analisi log `indicator_log` in DuckDB dopo 30-60 giorni di produzione
- Validare e ritunare soglie in `config/indicators.yaml` (attualmente "BEST-GUESS iniziali")
- Documentare soglie calibrate con motivazione in `docs/decisions.md`

### Sprint 10 — Case study / pubblicazione
**Dipendenza**: sistema stabile in produzione con dati sufficienti (Sprint 7-9)

- Raccolta risultati: figure CRPS, coverage, skill score vs NWP grezzo
- Pulizia repo per release pubblica (rimuovere credenziali, aggiungere LICENSE, README pubblico)
- Documentazione replica: come rieseguire l'esperimento
- Scrittura articolo LinkedIn/Medium
