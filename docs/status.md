# Guazza — Stato corrente

> Aggiornato: 2026-05-16

## Cosa è stato fatto

### Sprint 0 — Ricognizione (completato)
- Identificate 22 stazioni SIR, 6 stazioni ARPAT
- `config/stations.yaml` completo con coordinate, sensori verificati via API, `used_by` per location
- `config/sources.yaml`, `config/locations.yaml`, `config/indicators.yaml`, `config/arpat_levels.yaml` presenti

### Refactoring repo — schema wide + struttura flat (completato — 2026-05-15)
- **Schema DuckDB wide**: una riga per `(source, station_id, ts, granularity)`
  - `observations`, `forecasts`, `predictions`, `benchmark_forecasts` tutte wide
  - `granularity VARCHAR NOT NULL` in PK: distingue `daily` (CSV SIR) da `realtime` (actions.php, Netatmo)
  - `precip_interval_h TINYINT`: disambigua cumulato 24h (SIR storico) da misura 1h (realtime/Netatmo)
- **Eliminato sistema migrations**: `schema.sql` unico source of truth
- **Struttura flat**:
  - `src/guazza/fetchers.py` — SIR storico + realtime + Netatmo + Open-Meteo + ARPAT
  - `src/guazza/storage.py` — DuckDB client con lock file, `upsert_sir_observations` (batch), `upsert_forecasts`
  - `src/guazza/weights.py` — pesi stazioni (decay distanza + quota)
  - `src/guazza/indicators.py` — DLE (Decision Logic Engine)
  - `src/guazza/qc.py` — quality control osservazioni (SIR + ARPAT)
  - `src/guazza/jobs/ingest.py` — 4 comandi cron (historical/daily/realtime/forecasts)
  - `src/guazza/jobs/qc.py` — CLI QC run/report
  - **Scheletri per sprint futuri**: `models.py`, `output.py`, `jobs/predict.py`, `jobs/backup.py`
- `deploy/nginx.conf` + `deploy/Caddyfile`: configurazioni per il frontend statico (Sprint 6-7)

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

### Ingestion Open-Meteo (completato — 2026-05-16)
- `fetch_openmeteo_forecast`: fetch live multi-modello (ecmwf_ifs, ecmwf_aifs025, icon_eu, icon_d2, gfs025, arome_france)
- `fetch_openmeteo_historical`: backfill storico (Historical Forecast API, 2022+)
- Sostituito ECMWF 0.25° con HRES 9km e aggiunto ICON-D2 (2.2km) per migliorare risoluzione orografica (D-010).
- `ts_run` inferita per difetto all'ultimo run nominale del modello (ECMWF HRES: ogni 6h, ICON-D2: ogni 3h).
- `upsert_forecasts`: UPSERT batch, l'ultimo run vince

### Job di ingestion (completato — 2026-05-15)
Quattro comandi in `src/guazza/jobs/ingest.py`:

| Comando | Schedulazione | Cosa fa |
|---|---|---|
| `historical` | one-shot manuale | Backfill SIR CSV + Open-Meteo + ARPAT bollettini |
| `daily` | cron 06:00 UTC | Delta di ieri: SIR CSV + Open-Meteo historical |
| `realtime` | cron ogni 15-30 min | SIR actions.php + Netatmo tutte le location |
| `forecasts` | cron ogni 6h | Open-Meteo forecast multi-modello, 7 giorni |

Ogni job: `--dry-run`, Healthchecks.io ping (start/ok/fail), log JSON strutturato stdout, exit 1 su eccezione.

Flag aggiuntivi in `historical` e `daily`:
- `--only-sir`, `--only-openmeteo`, `--only-arpat`: esecuzione selettiva per sorgente
- `--location <id>`: limita a una o più location (ripetibile)

Flag aggiuntivi in `historical` e `daily`:
- `--only-sir`, `--only-openmeteo`, `--only-arpat`: esecuzione selettiva per sorgente
- `--location <id>`: limita a una o più location (ripetibile)

### Logging (completato — 2026-05-15)
- Loguru configurato con `serialize=True` su stdout nei comandi CLI (`_setup_logging()`)
- Non attivato a livello di modulo — i test pytest non sono impattati
- In produzione: aggiungere `logger.add(file, serialize=True, rotation="1 day")` in `_setup_logging()`

### Pulizia repo (completato — 2026-05-15)
- Rimossi `scripts/` (5 file Sprint 0) e `notebooks/` (gitkeep)
- Aggiornati `README.md`, `AGENTS.md`, `config/sources.yaml` per rimuovere ogni riferimento

## Test
- **156 test**, tutti verdi
- `ruff check` OK, `mypy` OK

## Prossimi passi (in ordine)

### Sprint 1b — ARPAT qualità aria (completato — 2026-05-15)
- `fetch_arpat_nrt()`: valori orari NO2/O3 da endpoint NRT ARPAT, `granularity='hourly'`
- `fetch_arpat_bollettini()`: PM10/PM2.5 giornalieri da endpoint bollettini, `granularity='daily'`
- `fetch_arpat_all_locations()`: wrapper per tutte le location con `arpat_stations` in config
- Integrato in job `realtime` (NRT) e `daily` (bollettini)
- Parsing difensivo: supporta formato lista e dict `{"stazioni": [...]}`
- `config/arpat_levels.yaml`: scale qualità aria ARPAT (PM10, PM2.5, NO2, O3, CO, benzene, SO2)
  con livelli normativi D.Lgs.155/2010 — usato dal DLE per calcolare il livello qualità aria
- 11 test pytest, tutti verdi
- CFR Toscana rimosso da `sources.yaml` (coperto da SIR idrometria)
- RainViewer marcato `scope: frontend_only` (nessun fetcher, usato solo in Sprint 6)

🟡 **Punto aperto**: struttura JSON reale degli endpoint ARPAT non verificata su rete reale.
Il parsing è difensivo (supporta più formati), ma da verificare al primo run su VPS.

### Sprint 2b — Backfill SIR pre-2022 (completato — 2026-05-16)

- Esteso `fetch_sir_historical` per scaricare CSV SIR oltre il 2022 (endpoint download.php supporta date precedenti)
- Verificato limite effettivo disponibilità archivio SIR per ogni stazione
- Caricato in DuckDB con stessa logica upsert esistente

### Sprint 2c — Check qualitativo dati SIR (completato — 2026-05-16)

- Eliminati dati pre-2004 (31.918 righe): pluviometro SIR non disponibile prima del 2004
- Tabella `quality_flags` in DuckDB: spike_tmin, spike_tmax, inversion_temp, range_precip_high
- `src/guazza/qc.py` + `src/guazza/jobs/qc.py` (CLI `run` / `report`)
- 153 flag su 166k righe (~0.09%): tutti meteorologicamente plausibili, nessun errore strumentale
- Nessuna inversione termica (tmin > tmax) — dati SIR sostanzialmente puliti
- 4 eventi precip > 150mm: eventi estremi reali (alluvione marzo 2015 inclusa)
- `temp_c` sempre NULL by design: SIR daily dà solo tmin/tmax — Sprint 3 usa tmin_c/tmax_c
- Finestra temporale utile per training: 2004–oggi (stazioni FI), 2007–oggi (casa_cesto/TOS11000516)

#### Miglioramenti QC (2026-05-16)

- **Transazione**: `compute_quality_flags` ora in `BEGIN TRANSACTION` / `COMMIT` / `ROLLBACK`
- **ARPAT flags**: 4 nuovi flag — `range_pm10_high` (>200 µg/m³), `range_pm25_high` (>100 µg/m³),
  `range_no2_high` (>400 µg/m³), `range_o3_high` (>300 µg/m³)
- **Realtime precip**: `range_precip_high` esteso a `granularity='realtime'` (CUM01, 1h cumulate)
- **Breakdown dict**: `compute_quality_flags` restituisce `{"total": N, "spike_tmin": M, ...}`
- **`--dry-run`**: aggiunto a `jobs/qc.py run`
- **Breakdown log**: il job logga il dettaglio per tipo flag
- 7 nuovi test (156 totali), mypy e ruff OK

### Sprint 3 — Feature Engineering (completato — 2026-05-16)

- `src/guazza/features.py`: `build_features_daily(db)` — training set materializzato in DuckDB
- `src/guazza/jobs/features.py`: CLI `build` / `info`
- Schema training set: `(location_id, target_date, lead_time_h)` con:
  - NWP: 4 modelli × 5 variabili (tmin, tmax, precip, humidity, wind) — pivot wide
  - Ensemble stats: media null-safe + spread (DuckDB GREATEST/LEAST ignora NULL)
  - NWP orario → daily: MIN(temp)=tmin, MAX(temp)=tmax, SUM(precip), AVG(humidity/wind)
  - Obs features: SIR pesato del giorno precedente (lookahead-safe)
  - Climatologia: media/std mensile multi-anno da obs_weighted
  - Calendario: month, day_of_year
  - Target: SIR pesato a target_date (ground truth)
- 10 test pytest, tutti verdi
- **Risultato live**: 19.155 righe, 4 location, 2022-01-02→oggi, >99% target coverage

🟡 **Punto aperto — lead_time_h range 1-11h** (atteso fino a 168h):
il backfill Open-Meteo ha salvato solo il run più recente per valid time, non la storia dei run.
In produzione il fetch giornaliero accumulerà run a distanze crescenti. Da verificare dopo
il primo mese di operatività sul VPS (Sprint 7).

🟡 **Punto aperto — feature upstream pluviometriche**: aggiungere stazioni Lucca/Pistoia/Versilia
a `stations.yaml` come `upstream_pluvio` (senza `used_by`) e rieseguire `features build`.
Non bloccante per Sprint 4 — si aggiungono come feature incrementali.

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
- Crontab con i 4 job ingestion + `qc run` + job `predict`
- Configurazione `.env` produzione (Netatmo, Healthchecks.io), `load_dotenv` per lettura DB_PATH e HEALTHCHECKS_URL
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
