# Changelog

Tutte le modifiche rilevanti al progetto sono documentate qui.
Formato: [Keep a Changelog](https://keepachangelog.com/it/1.0.0/).
Versioning: major per sprint, minor per milestone interne.

---

## [Unreleased]

### Added
- `analysis/baseline_backtest.py`: baseline backtest D+0 (read-only) per de-risking della
  tesi. Conferma bias di microclima sistematico e correggibile già a D+0 (es. `casa_cesto`
  fondovalle: tmin sovrastimata da tutti i 6 NWP). Floor di skill per il modello ML.
- `docs/decisions.md` D-016: il baseline di confronto per le claim di skill è il
  multimodello-mean per-location (baseline naive più forte), non il singolo NWP.

### Changed
- Stack: il 3050 diventa host Proxmox (homelab multi-servizio), Guazza è un tenant.
  Scheduling riformulato come "cron o k8s CronJob" — l'invariante blindato è che i job
  restino CLI idempotenti **orchestrator-agnostic**, non l'uso obbligato di cron.
- Anti-pattern: rimosso il ban duro su Kubernetes/ArgoCD (era un vincolo sul *target di
  deploy*); resta vietato l'**accoppiamento** della logica app a un orchestratore
  (Prefect/Dagster/Airflow/Celery) e l'esposizione come PaaS. Aggiunto "Invariante deploy"
  con i vincoli tecnici DuckDB su k8s (single-writer, PVC RWO local-path, backup CronJob).
- Docs allineate alla decisione: `AGENTS.md` (stack + anti-pattern), `README.md`
  (architettura), `docs/decisions.md` (D-007), `docs/status.md` (Sprint 8), terminologia
  "VPS" → "server homelab" in `docs/known_issues.md`.

### Fixed
- _(nessuno)_

---

## [0.7.0] — 2026-05-29

### Changed
- **Redesign frontend v2 — CSS custom**: rimossi Tailwind CSS e DaisyUI. Il frontend usa
  ora CSS custom (`style.css`, classi `g-*`), senza framework né build step. Nuova palette
  "Carbone e Iride" (4 livelli superficie carbone + accento iris `#6B7FD4` + 5 segnali
  semantici), tipografia Geist (display) + JetBrains Mono (dati numerici) via Google Fonts CDN.
- **Attribution RainViewer** riposizionata nella mappa radar.

### Added
- **`DESIGN.md`**: design system frontend completo (colori, tipografia, elevazione,
  componenti, do's & don'ts).
- **`PRODUCT.md`**: product brief (utenti, scopo, brand personality, principi di design,
  anti-references).
- **Campo `mean`** (E[precip]) esposto nelle previsioni JSON (`output.py`).

### Docs
- Allineati `README.md`, `AGENTS.md`, `docs/status.md` e `config/sources.yaml` al codice:
  stack frontend (CSS custom, no Tailwind/DaisyUI), sorgente qualità aria (ARPAT OpenData
  NRT come sorgente attiva, OpenAQ marcato storico), conteggio test (241).

---

## [0.6.3] — 2026-05-22

### Changed
- **Sostituito fetcher qualità aria OpenAQ con ARPAT OpenData NRT**: endpoint
  pubblico senza auth (`/json_orari_nrt/{STATION}/{DD-MM-YYYY}`). Lista statica
  stazioni da `locations.yaml` (arpat_stations). Source nel DB: `arpat`. CO in
  mg/m³ nativo (D.Lgs.155/2010), nessuna conversione.
- Rimosso `--only-openaq` da `historical` (nessun endpoint range disponibile).
- `get_current_air_quality()`: filtro `source='arpat'` (era `'openaq'`).
- QC `_insert_range_arpat_flags`: filtro `source='arpat'`.

### Fixed
- **KI-017 chiuso**: recuperate stazioni AR-ENELSB-SANGIOVANNI (BENZENE/CO,
  casa_cesto) e FI-LAVAGNINI (NO2, casa_nicco), assenti da OpenAQ.

---

## [0.6.2] — 2026-05-20

### Changed
- **Sostituito fetcher qualità aria ARPAT con OpenAQ v3**: discovery dinamica
  per coordinate (lat/lon + raggio 15km) — nessuna lista statica di stazioni.
  Auth via `OPENAQ_API_KEY` (`X-API-Key` header). Source nel DB: `openaq`.
  CO convertito da µg/m³ (OpenAQ) a mg/m³ (schema). 9/10 stazioni ARPAT
  precedenti coperte; vedi KI-017 per le 2 mancanti.
- **AQ realtime only**: rimosso fetch giornaliero bollettini PM10/PM2.5.
  `get_current_air_quality()` legge solo `granularity='hourly'` con finestra 3h.
  Nessun backfill storico (AQ non è feature di training).
- **Frontend — qualità aria sempre visibile**: `renderAirQuality()` mostra
  tutti e 7 i parametri (PM10, PM2.5, NO₂, O₃, CO, C₆H₆, SO₂); valori
  mancanti come `—` con colore attenuato, griglia fissa a 7 colonne.

### Fixed
- **OpenAQ `/locations/{id}/latest` non include `parameter` nei risultati**:
  costruito mapping `sensor_id → (param, units)` dalla discovery; usato
  `sensorsId` (int) per lookup. Senza questo fix `fetch_openaq_latest`
  restituiva sempre 0 righe.
- **PK collision quando stazioni OpenAQ cadono nel raggio di più location**:
  `station_id` ora include location_id (`openaq_{id}_{location_id}`) — la
  PK `(source, station_id, ts, granularity)` non include location_id, e
  senza questo fix l'ultima location processata sovrascriveva le altre.
- **Timestamp OpenAQ disallineati con `CURRENT_TIMESTAMP` di DuckDB**:
  timestamp UTC parsati dall'API venivano salvati naive UTC mentre
  `CURRENT_TIMESTAMP` usa il timezone locale, escludendo righe valide
  dalla finestra 3h su macchine non-UTC. Ora convertiti a ora locale
  naive (Europe/Rome) prima del salvataggio, coerente con SIR.

### Removed
- Codice ARPAT: `_ARPAT_NRT_URL`, `_ARPAT_BOLLETTINI_URL`, `_ARPAT_NRT_VAR_MAP`,
  `_ARPAT_BOLL_VAR_MAP`, `_fetch_arpat_json`, `fetch_arpat_nrt`,
  `fetch_arpat_bollettini`, `fetch_arpat_bollettini_range`,
  `fetch_arpat_all_locations` (~400 righe da `fetchers.py`).
- 23.218 righe `source='arpat'` cancellate da `observations` in locale
  (vedi KI-016 per la stessa pulizia su VPS).

---

## [0.6.1] — 2026-05-18

### Added
- **Quinta location `casa_nicco`** (Firenze Novoli, 43.791/11.219, 40m)
  - Primaria SIR: TOS01001096 Firenze Università; termo pesato su TOS03001097 Orto Botanico (ΔQ+8m)
  - 4 stazioni ARPAT Firenze: FI-MOSSE (0.50), FI-BOBOLI (0.30), FI-GRAMSCI (0.15), FI-LAVAGNINI (0.05)
  - Tab frontend aggiunta
- **Qualità aria ARPAT nel pannello realtime**: `get_current_air_quality()` in `output.py`
  (PM10/PM2.5/benzene da bollettini, NO2/O3/CO/SO2 da NRT), campo JSON top-level `air_quality`,
  `renderAirQuality()` + `AQ_THRESHOLDS` nel frontend con card e colori da soglie ARPAT
- **CO, benzene, SO2 nella pipeline ARPAT**: erano nella risposta API ma scartati;
  3 colonne nuove in `observations` (`co_mgm3`, `benzene_ugm3`, `so2_ugm3`),
  migrazione idempotente `_ensure_aq_columns()`

### Changed
- **Frontend — grafico tendenza day-scoped**: `buildChartPoints` filtra al giorno selezionato; asse X fisso 00-23h sempre; grafico vuoto (non nascosto) se il modello non ha dati per quel giorno
- **Frontend — animazione transizione**: fade-in + slide-up 200ms su ogni cambio location o giorno

### Removed
- **Indicatore DLE `aria`**: la qualità aria è un dato osservativo, non un semaforo
  previsionale — `config/indicators.yaml` passa da 9 a 8 indicatori
- **`frontend-v1/`**: vecchio frontend, sostituito da `frontend/`
- **`today_hourly`** dal payload JSON e `get_today_hourly()` da `output.py`: non più
  consumato dal frontend dopo il fix del grafico tendenza

### Fixed
- **Frontend — grafico tendenza vuoto all'apertura**: `buildChartPoints` per il giorno
  corrente usava `today_hourly` (solo ore future di oggi → vuoto a fine giornata);
  ora usa sempre `days[].hourly` (profilo 24h) per il modello `guazza`
- **Frontend — crash crosshair su Edge**: `chart.tooltip._active` con optional chaining;
  `chart.tooltip` è `undefined` durante i primi `afterDraw` su Edge
- **Frontend — card border clipping**: `ring-2` della card attiva veniva tagliato da `overflow-x-auto`; fix `p-1` sul wrapper scrollabile
- **SIR historical — sleep inutile**: rimosso `time.sleep(1.0)` da `_fetch_one`; il server SIR serializza le connessioni lato server, il sleep aggiungeva solo overhead (~28s su backfill completo)
- **Docs**: comando corretto `predict` (non `predict run`) in AGENTS.md, README e status.md

---

## [0.5.0] — 2026-05-17

Sprint 5 completato: pipeline predict end-to-end operativa.
JSON per ogni location con CI quantile + indicatori DLE + coverage rolling.

### Added
- **`src/guazza/output.py`**: signal bridge + JSON writer
  - `build_signals()`: ML quantile → `SignalBag` DLE (CDF inversa lineare per tmin/tmax/precip; NWP ensemble per vento/umidità)
  - `compute_coverage_30d()`: copertura empirica rolling 30 giorni (`null` se < 10 campioni)
  - `write_location_json()`: `{output_dir}/{location_id}.json` con `forecasts`, `indicators`, `coverage_empirical_30d`
- **`src/guazza/jobs/predict.py`**: job cron `predict run`
  - Pipeline: `ensure_predictions_schema` → `backfill_prediction_obs` → `features_daily` → `predict` → `upsert_predictions` → DLE → `indicator_log` → JSON
  - Flag `--dry-run`, Healthchecks.io, logging JSON su cron
- **`upsert_predictions()`** in `storage.py`: bulk upsert DataFrame Arrow (3 target × 9 colonne)
- **`backfill_prediction_obs()`** in `storage.py`: UPDATE bulk `*_obs` da `obs_weighted` per coverage tracking
- **`ensure_predictions_schema()`** in `storage.py`: migrazione automatica schema v0.4→v0.5
- **18 test** `test_output.py`: `_prob_exceeds`, `build_signals`, `compute_coverage_30d`, `write_location_json`

### Changed
- **`schema.sql`**: tabella `predictions` riscritta (3 target × p05/p10/p50/p90/p95 + ci80/ci90 + obs, 30 colonne)
- **`OUTPUT_DIR`**: default `data/output/` (via env `OUTPUT_DIR`)

---

## [0.4.1] — 2026-05-17

Ring features upstream pluviometrici + ottimizzazione staging Arrow.

### Added
- **Ring features upstream** (6 colonne): `ring{1,2,3}_precip_d1_{mean,max}` in `features_daily`.
  Stazioni SIR raggruppate in fasce di distanza (ring1 ≤20km, ring2 ≤50km, ring3 ≤100km)
  per ogni location — segnale anticipatore precipitazioni su microclimi orografici
- **`upstream_ring_station`** in `schema.sql`: tabella `(station_id, location_id, ring_label, distance_km)`
- **`refresh_upstream_rings()`** in `weights.py`: assegnazione automatica ring per distanza su tutte
  le stazioni con sensore pluviometro in `stations.yaml`
- **13 nuove stazioni upstream** in `stations.yaml` + `locations.yaml`: Bisenzio/Appenino PO-PT
  (Vaiano, Fattoria Iavello, Santomato, Cantagallo, Acquerino, Gavigno, Vernio),
  storm track W (Albano, Bagni di Lucca, Montecarlo), Valdarno/Pratomagno
  (Pian di Scò, Pratomagno, Trappola)
- **`register_df()` / `unregister_df()`** su `DuckDBClient`: API pubblica per il path Arrow

### Changed
- **`features_daily`**: 6 colonne ring aggiunte; `FEATURE_COLS` in `models.py` passa da 44 a 50
- **`storage.py`**, **`weights.py`**, **`indicators.py`**, **`fetchers.py`**: tutti gli `executemany`
  di bulk insert sostituiti con `pd.DataFrame` + `conn.register()` (path Arrow DuckDB).
  Atteso 10–50x speedup su batch grandi (KI-010 risolto)
- **Modello retrain** su 6384 righe con 50 feature (ring inizialmente NULL, gestite nativamente da LightGBM)

---

## [0.4.0] — 2026-05-17

Sprint 4 completato: primo modello ML trainato e calibrato su dati reali.
Skill temperature +25–27% vs NWP ensemble. CQR coverage ~0.90 su target 90%.

### Added
- **`models.py`** (Sprint 4): LightGBM quantile regression (α = 0.05/0.10/0.50/0.90/0.95)
  per 3 target (tmin_c, tmax_c, precip_mm). CQR stratificato per 6 bucket lead time
  (D-002, D-003). Walk-forward CV con embargo 7 giorni. Persistenza pickle in `data/models/`
- **`jobs/train.py`**: CLI `train run` (allena + salva artefatti) e `train eval`
  (walk-forward CV con metriche MAE, CRPS, coverage, skill)
- **ICON-2I** (D-013): `italia_meteo_arpae_icon_2i` aggiunto come 6° modello NWP
  (2.2km, assimila osservazioni italiane, orizzonte 72h). `features_daily` ora a 54 colonne
- **`--om-model`**: flag ripetibile in `historical` e `daily` per limitare il download
  a uno o più modelli Open-Meteo specifici
- **11 test** `test_models.py` con fixture `fast_lgbm` (n_estimators=50, <60s totali)

### Changed
- **`features.py`**: ensemble mean/spread esteso da 5 a 6 modelli con formula
  null-safe (COALESCE + divisore dinamico) — gestisce icon2i NULL per lead_time_h > 72h
- **D-014**: per la precipitazione il DLE userà la distribuzione ensemble NWP
  direttamente (skill ML ≈ 0 su CV reale, vedi decisions.md)

### Fixed
- `storage.py`: UTC-naive normalizzation prima della staging — previene `Constraint Error`
  su DST transition days (KI-009)
- `features.py`: gestione `lead_time_h=0` per backfill same-day (ts_run collassato a NULL)
- `fetchers.py`: rimosso `ecmwf_aifs025` — restituisce null su tutte le variabili (KI-011)

### Removed
- `ecmwf_aifs025` da `_OM_MODELS` e da tutta la codebase

---

## [0.3.0] — 2026-05-16

Sprint 3 completato: feature engineering + QC migliorato.
Training set materializzato in DuckDB con 44 feature (NWP, ensemble stats, obs, climatologia, calendario).

### Added
- **`features.py`**: `build_features_daily()` — tabella `features_daily` con schema
  `(location_id, target_date, lead_time_h)`, 4 modelli NWP × 5 variabili, ensemble mean/spread,
  obs SIR pesate del giorno precedente (lookahead-safe), climatologia mensile multi-anno,
  features calendario (month, day_of_year)
- **`jobs/features.py`**: CLI `features build` e `features info`
- **QC ARPAT** in `qc.py`: 4 nuovi flag — `range_pm10_high`, `range_pm25_high`,
  `range_no2_high`, `range_o3_high`
- **QC realtime precip**: `range_precip_high` esteso a `granularity='realtime'`
- **`--dry-run`** in `jobs/qc.py run`

### Changed
- `compute_quality_flags`: ora in `BEGIN TRANSACTION` / `COMMIT` / `ROLLBACK`
- `compute_quality_flags`: restituisce breakdown dict per tipo flag; il job logga il dettaglio

### Fixed
- Progress bar nested su fetch storico Open-Meteo; logging aligned tra tutti i fetcher
- `storage.py`: `INSERT OR REPLACE` sostituisce UPDATE+INSERT NOT EXISTS in `upsert_forecasts`

---

## [0.2.0] — 2026-05-16

Sprint 1b (ARPAT qualità aria), Sprint 2b (backfill SIR pre-2022) e Sprint 2c (QC) completati.

### Added
- **`fetchers.py`** — ARPAT: `fetch_arpat_nrt()` (valori orari NO2/O3, `granularity='hourly'`),
  `fetch_arpat_bollettini()` (PM10/PM2.5 giornalieri, `granularity='daily'`),
  `fetch_arpat_all_locations()` (wrapper multi-location)
- **`config/arpat_levels.yaml`**: scale qualità aria D.Lgs.155/2010 (PM10, PM2.5, NO2, O3, CO, benzene, SO2)
- **`jobs/ingest.py`**: integrazione ARPAT in `realtime` (NRT) e `daily` (bollettini);
  parallelizzazione fetch SIR + Open-Meteo con `ThreadPoolExecutor`
- **Sprint 2b**: backfill SIR CSV oltre il 2022 — endpoint `download.php` supporta date precedenti
- **`quality_flags`** in `schema.sql`: tabella spike/range con flag per SIR e ARPAT
- **`qc.py`**: `compute_quality_flags()` — 4 flag SIR (`spike_tmin`, `spike_tmax`,
  `inversion_temp`, `range_precip_high`)
- **`jobs/qc.py`**: CLI `qc run` e `qc report`
- **`--only-sir`**, **`--only-openmeteo`**, **`--only-arpat`**, **`--location`**:
  flag selettivi in `historical` e `daily`
- **`load_dotenv`** in `ingest.py` per `DB_PATH` locale

### Changed
- `upsert_sir_observations`: refactoring da `executemany` a staging table con dedup
- Dati pre-2004 eliminati (31.918 righe): pluviometro SIR non disponibile prima del 2004
- CFR Toscana rimosso da `sources.yaml` (coperto da SIR idrometria)

### Fixed
- Parser ARPAT bollettini + NRT adattato al formato reale API (`{"stazioni": [...]}`)
- Endpoint ARPAT aggiornato a `api.arpat.toscana.it`
- `upsert_sir_observations`: dedup batch prima di `executemany` (previene violazioni PK)
- Chunk dinamico per modello ad alta risoluzione (ICON-D2, AROME: 90gg; altri: 180gg)
- Rispetto di `Retry-After` su 429 Open-Meteo Historical

---

## [0.1.0] — 2026-05-15

Prima versione stabile dello Sprint 1: ingestion completa (SIR + Netatmo + Open-Meteo),
schema DuckDB wide, job cron operativi.

### Added
- **Schema DuckDB wide** (`schema.sql`): tabelle `observations`, `forecasts`, `predictions`,
  `benchmark_forecasts`, `station_weights`, `netatmo_fetch_log`, `indicator_log`
- **`granularity` in PK `observations`**: distingue `daily` (CSV SIR) da `realtime`
  (actions.php, Netatmo) — previene sovrascritture silenti a mezzanotte
- **`precip_interval_h`** in `observations`: disambigua cumulato 24h da misura 1h
- **`fetchers.py`**: fetch SIR storico CSV, SIR realtime, Netatmo (con QC a 3 livelli),
  Open-Meteo forecast e historical multi-modello (ECMWF, ICON-EU, GFS, AROME)
- **`storage.py`**: `DuckDBClient` con lock file (`fcntl.flock`), `upsert_sir_observations`
  (batch COALESCE), `upsert_forecasts` (batch, ultimo run vince)
- **`weights.py`**: calcolo pesi stazione→location (decay distanza + quota)
- **`indicators.py`**: Decision Logic Engine (DLE) con costo asimmetrico fn/fp
- **`jobs/ingest.py`**: 4 comandi cron — `historical`, `daily`, `realtime`, `forecasts`
- **Logging JSON**: `_log_scrape()` e `_setup_logging()` con loguru `serialize=True` su stdout
- **Healthchecks.io**: ping start/ok/fail in ogni job
- **118 test** pytest, ruff OK, mypy OK

### Changed
- Struttura repo flat: eliminati 6 package vuoti (`ingestion/`, `storage/`, `features/`,
  `models/`, `output/`, `evaluation/`) → file singoli in `src/guazza/`
- `init_schema()`: esegue script SQL intero invece di split su `;`
- `upsert_sir_observations`: refactoring da loop insert singolo a batch `executemany`
- SIR realtime: timestamp parsato dal campo `date` nel JSON (era `now(UTC)`)
- Rimossi `scripts/` (Sprint 0) e `notebooks/`

### Fixed
- `weights.py`: `quota_m=0` non più trattato come falsy (`is not None`)
- `storage.py`: `hum_med_pct=0.0` non più trattato come falsy (regressione `or`)
- `fetchers.py`: `INTERVAL` SQL via f-string (era concatenazione stringa+parametro fragile)
- SIR realtime: aggiunto header `Referer` obbligatorio (senza risponde con HTML)

---

## [0.0.1] — 2026-05-13

Bootstrap iniziale del progetto.

### Added
- Struttura repo, `pyproject.toml`, configurazione ruff/mypy/pytest
- `config/locations.yaml`: 4 location con coordinate, stazioni SIR, ARPAT
- `config/stations.yaml`: 22 stazioni SIR + 6 ARPAT con sensori verificati
- `config/sources.yaml`: endpoint sorgenti dati con stato di accesso
- `config/indicators.yaml`: definizione indicatori operativi (panni, gelata, motorino, ecc.)
- `docs/decisions.md`: decisioni architetturali motivate
- `docs/known_issues.md`: problemi noti e workaround
- Agenti subagent per collaborazione multi-modello (Claude, Kimi)

[Unreleased]: https://github.com/cosimo/guazza/compare/v0.6.1...HEAD
[0.6.1]: https://github.com/cosimo/guazza/compare/v0.6.0...v0.6.1
[0.5.0]: https://github.com/cosimo/guazza/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/cosimo/guazza/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/cosimo/guazza/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/cosimo/guazza/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/cosimo/guazza/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cosimo/guazza/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/cosimo/guazza/releases/tag/v0.0.1
