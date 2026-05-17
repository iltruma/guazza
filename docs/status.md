# Guazza — Stato corrente

> Aggiornato: 2026-05-17 (Sprint 5 completato)

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
- `fetch_openmeteo_forecast`: fetch live multi-modello (ecmwf_ifs, icon_eu, icon_d2, gfs025, arome_france, italia_meteo_arpae_icon_2i). `ecmwf_aifs025` rimosso: null su tutte le variabili (KI-011).
- `fetch_openmeteo_historical`: backfill storico (Historical Forecast API, 2022+).
- **Batching Coordinate**: Implementato l'invio di multiple coordinate in una singola chiamata API. Ridotti i round-trip HTTP del 75%.
- **Temporal Chunking**: Aggiunto frazionamento delle richieste storiche (chunk di 180 giorni) per evitare timeout lato server su modelli ad alta risoluzione.
- **Chunk dinamico per modello**: icon_d2 e arome_france usano chunk da 90gg; gli altri modelli 180gg. Previene HTTP 504 su modelli convettivi ad alta risoluzione.
- **`_fetch_om_json_historical`**: funzione separata dal forecast live con timeout 90s, 5 retry, backoff max 60s.
- Sostituito ECMWF 0.25° con HRES 9km e aggiunto ICON-D2 (2.2km) per migliorare risoluzione orografica (D-013).
- `ts_run` inferita per difetto all'ultimo run nominale del modello (ECMWF HRES: ogni 6h, ICON-D2: ogni 3h).
- `upsert_forecasts`: UPSERT batch, l'ultimo run vince.

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
  (`daily` espone solo `--only-sir` e `--only-openmeteo`)
- `--location <id>`: limita a una o più location (ripetibile)
- `--om-model <nome>`: limita Open-Meteo a uno o più modelli specifici (ripetibile). Es:
  `historical --only-openmeteo --om-model italia_meteo_arpae_icon_2i`

### Logging (completato — 2026-05-15)
- Loguru configurato con `serialize=True` su stdout nei comandi CLI (`_setup_logging()`)
- Non attivato a livello di modulo — i test pytest non sono impattati
- In produzione: aggiungere `logger.add(file, serialize=True, rotation="1 day")` in `_setup_logging()`

### Pulizia repo (completato — 2026-05-15)
- Rimossi `scripts/` (5 file Sprint 0) e `notebooks/` (gitkeep)
- Aggiornati `README.md`, `AGENTS.md`, `config/sources.yaml` per rimuovere ogni riferimento

## Test
- **176 test** (165 pre-Sprint 4 + 11 test_models), tutti verdi in ~55s
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

**Fix same-day (2026-05-17)**: il SQL di `build_features_daily` gestisce correttamente
`lead_time_days=0` (backfill storico: ts_run e ts_valid sullo stesso giorno). Questi record
vengono aggregati sull'intera giornata (tmin=MIN, tmax=MAX, precip=SUM) con ts_run collassato
a NULL nel GROUP BY — coerente con le osservazioni SIR daily.

**Ring features upstream completate (2026-05-17)**:
- `upstream_ring_station` in schema DuckDB: mapping station→location con ring_label e distance_km
- 13 stazioni upstream pluvio in `stations.yaml` (Bisenzio/Appenino PO-PT, storm track W, Valdarno/Pratomagno)
- `refresh_upstream_rings()` in `weights.py`: assegnazione automatica ring1/ring2/ring3 per distanza
- 6 nuove feature in `features_daily`: `ring{1,2,3}_precip_d1_{mean,max}` (lookahead-safe: giorno precedente)
- Modello retrain con 50 feature totali (44 + 6 ring)

### Sprint 4 — Modello ML (completato — 2026-05-17)

- `src/guazza/models.py`: `train_all`, `walk_forward_cv`, `predict` con CQR stratificato 6 bucket lead time
- `src/guazza/jobs/train.py`: CLI `train run` e `train eval`
- 11 test pytest, fixture `fast_lgbm` (n_estimators=50) per contenere il tempo sotto 60s
- Artefatti persistiti in `data/models/artifacts.pkl`

#### Risultati walk-forward CV (4 fold, 2023-01 → 2026-05, dati reali)

| Target | MAE | CRPS | Coverage 80% | Coverage 90% | Skill vs NWP |
|---|---|---|---|---|---|
| tmin_c | 0.906°C | 0.500 | 0.794 | 0.896 | +25.6% |
| tmax_c | 0.801°C | 0.452 | 0.816 | 0.912 | +26.9% |
| precip_mm | 1.526mm | 0.924 | 0.788 | 0.908 | -1.2% |

**Temperatura**: skill +25–27% vs ensemble NWP mean. CQR calibrato (coverage ~0.90 su target 90%).
**Precipitazione**: skill ≈ 0 — il modello pareggia il NWP grezzo ma non lo batte (vedi D-014).

#### CQR corrections produzione (cal set 2026-02-14 → 2026-05-15, 364 righe)

| Target | ci80 | ci90 |
|---|---|---|
| tmin_c | +0.320°C | +0.486°C |
| tmax_c | +0.405°C | +0.519°C |
| precip_mm | +0.006mm | +0.009mm |

🟡 **Punto aperto — benchmark_forecasts**: tabella prevista per confronto sistematico in produzione
(NWP grezzo vs modello nel tempo). Non implementata — da aggiungere in Sprint 5 o come task separato.

### Sprint 5 — Output JSON + Decision Logic Engine (completato — 2026-05-17)

- `src/guazza/output.py`: `build_signals()`, `compute_coverage_30d()`, `write_location_json()`
- `src/guazza/jobs/predict.py`: job cron `predict run` — modello → DB → DLE → JSON
- `src/guazza/storage.py`: `ensure_predictions_schema()`, `upsert_predictions()`, `backfill_prediction_obs()`
- `schema.sql`: tabella `predictions` v0.5 (3 target × 9 quantili/CI + `*_obs`)
- 18 test pytest, mypy e ruff OK
- **Pipeline end-to-end verificata**: 4 JSON in `data/output/`, 9 indicatori per location

**Signal bridge**: ML quantile → CDF inversa lineare per tmin/tmax/precip; NWP ensemble empirico per vento/umidità.
**`coverage_empirical_30d`**: tutti `null` al primo run — si popola dopo il primo mese operativo (via `backfill_prediction_obs`).

🟡 **Punto aperto — `bisenzio` threshold**: `threshold_1`/`threshold_2` non sono nel SignalBag → fallback giallo.
   Nessuna sorgente dati per le soglie idrometriche di allerta SIR. Da implementare in Sprint 7+ (config o API SIR).

### Pre-Sprint 6 — task da completare prima del frontend

🟡 **Soglie idrometriche `bisenzio`** (KI-012): aggiungere `threshold_1`/`threshold_2`
per la stazione TOS01004791 (S. Piero a Ponti) in `config/locations.yaml` o `indicators.yaml`,
e popolare il SignalBag in `build_signals()`. Sblocca l'indicatore dal fallback giallo.

🟡 **Predict job multi-giorno**: ora genera un solo JSON per la data più lontana disponibile
(`MAX(target_date)`). Serve iterare tutte le date future (D+1…D+7) e includerle nel JSON
come array `days`. Necessario per il frontend Sprint 6 e per avere la previsione di domani.
`write_location_json` e `build_signals` sono già pronti; cambia solo il loop in `jobs/predict.py`.

🟡 **Profilo orario (disaggregazione NWP)**: aggiungere array `hourly` (24 elementi) al JSON
di output. Temperatura: rescaling lineare del profilo NWP ensemble-mean ancorato a tmin/tmax ML.
Precipitazione: NWP scalato proporzionalmente a `precip_p50` + `precip_prob` (frazione modelli
con precip > 0.1mm per ora). Nessuna modifica a DB o modelli — calcolato al momento della
scrittura JSON dalla tabella `forecasts` esistente. Implementare in `output.py` +
`jobs/predict.py` + nuovi test.

### Sprint 6 — Frontend
**Dipendenza**: JSON output Sprint 5 stabile + pre-Sprint 6 completato

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
