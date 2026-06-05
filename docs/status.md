# Guazza — Stato corrente

> Aggiornato: 2026-06-05 (riconciliazione baseline skill D-016 + verdetto DLE grigio, v0.8.1)

## Cosa è stato fatto

### Sesta location casa_cercina + accumulo Netatmo daily (2026-06-02)

**casa_cercina** (Sesto Fiorentino, versante S di Monte Morello, 311m) — prima
location a quota collinare. Tutte le SIR vicine sono nel catino fiorentino
(ΔQ -200/-280m); l'unica a quota comparabile è Vaiano (TOS11000503, 322m, ΔQ+11m,
13.7km, valle Bisenzio). Poiché Open-Meteo downscala già la temperatura a 311m,
ancorare il termo a una SIR di pianura sarebbe un train/serve skew in quota →
**termo ancorato a Vaiano** (`termo: [TOS11000503]`); pluvio/anemo su vicine di
pianura; nessuna idrometrica; ARPAT FI-MOSSE/FI-LAVAGNINI. Dettaglio in D-018.

- `config/locations.yaml`: blocco `casa_cercina`; `config/stations.yaml`: `used_by`
  aggiornato su 8 stazioni; `frontend/app.js`: tab "Casa Cercina".
- **Accumulo Netatmo daily** (`src/guazza/netatmo_daily.py` + job
  `guazza.jobs.netatmo_daily`): aggrega il realtime Netatmo in righe
  `granularity='daily'` (tmin/tmax/humidity, giorno locale Europe/Rome). **Non**
  entra nel training (`features.py` resta `source='sir_toscana'`): costruisce lo
  storico per stimare in Sprint 9+ l'offset Cercina↔Vaiano. Agganciato al job
  `daily`. Precip non aggregata (overlap `rain_1h`); tmax grezza ma con bias
  solare noto. 6 test in `tests/test_netatmo_daily.py`.
- **Fix qualità aria (KI-021)**: `get_current_air_quality` risolve le stazioni via
  JOIN `station_weights` (`source='arpat'`) invece di `observations.location_id` —
  robusto a stazioni ARPAT condivise tra location (la PK observations non include
  location_id). `weights refresh` ora popola anche i pesi ARPAT dal config. Media
  pesata per stazione. 4 test nuovi (`test_output.py`, `test_weights.py`).

**Onboarding live casa_cercina completato (2026-06-05)**: `weights refresh` →
   `ingest historical` (SIR + OM) → `features build` → `train run` → `ingest forecasts`
   + `realtime` → `predict`. `data/output/casa_cercina.json` generato, 4 giorni
   (D+0…D+3, lead 72-144h — si estende man mano che i cron accumulano run più recenti).

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
| `historical` | one-shot manuale | Backfill SIR CSV + Open-Meteo |
| `daily` | cron 06:00 UTC | Delta di ieri: SIR CSV + Open-Meteo historical |
| `realtime` | cron ogni 15-30 min | SIR actions.php + Netatmo + ARPAT NRT tutte le location |
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
- **241 test**, tutti verdi
- `ruff check` OK, `mypy` OK

## Prossimi passi (in ordine)

### Sprint 1b — ARPAT qualità aria (completato — 2026-05-15, aggiornato 2026-05-22)
- `fetch_arpat_all_locations()`: wrapper per tutte le location con `arpat_stations` in config
- Integrato in job `realtime` (NRT orario, ogni 30 min)
- Parsing: lista di dict orari con `ORA`/`DATA_OSSERVAZIONE` e valori numerici/null
- `config/arpat_levels.yaml`: scale qualità aria (PM10, PM2.5, NO2, O3, CO, benzene, SO2),
  livelli normativi D.Lgs.155/2010 — usato dal frontend per colori qualità aria
- 14 test pytest, tutti verdi
- **2026-05-22**: rimosso fetcher OpenAQ (valori inaffidabili); sostituito con ARPAT OpenData NRT
  (`https://opendata.arpat.toscana.it/.../json_orari_nrt/{STATION}/{DD-MM-YYYY}`). Recuperate
  stazioni AR-ENELSB-SANGIOVANNI e FI-LAVAGNINI mancanti da OpenAQ (KI-017 chiuso).
  `source='openaq'` → `source='arpat'` in `observations`, `qc.py`, `output.py`.

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
il primo mese di operatività sul server locale (Sprint 8).

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

#### Risultati walk-forward CV (4 fold, 2023-01 → 2026-06, 6 modelli — ri-eseguito 2026-06-05)

| Target | MAE | Coverage 80% | Coverage 90% | Skill vs NWP-mean |
|---|---|---|---|---|
| tmin_c | 0.905°C | 0.801 | 0.909 | +32.5% |
| tmax_c | 0.821°C | 0.826 | 0.913 | +42.8% |
| precip_mm | 1.592mm | 0.814 | 0.908 | -2.4% |

**Temperatura**: skill +32/+43% vs ensemble NWP mean (target pesato). CQR calibrato (coverage ~0.90 su target 90%).
**Precipitazione**: skill ≈ 0 — il modello pareggia il NWP grezzo ma non lo batte (vedi D-014).
Lo skill è salito rispetto al +25–27% storico (4 modelli): aggiungere GFS/AROME/ICON-2I peggiora
l'ensemble-mean grezzo ma il modello, che li usa come feature, regge → vedi riconciliazione baseline (D-016).

#### CQR corrections produzione (cal set 2026-02-14 → 2026-05-15, 364 righe)

| Target | ci80 | ci90 |
|---|---|---|
| tmin_c | +0.320°C | +0.486°C |
| tmax_c | +0.405°C | +0.519°C |
| precip_mm | +0.006mm | +0.009mm |

**benchmark_forecasts implementata (2026-06-05)**: confronto sistematico NWP grezzo vs ML nel
tempo. `ensure_benchmark_schema()` migra il vecchio schema; `upsert_benchmark_forecasts()` e
`backfill_benchmark_obs()` in `storage.py`; `jobs/predict.py` popola una riga per (source,
location, target_date) ad ogni run da `nwp_comparison`. Si accumula dal deploy in poi.

### Sprint 5 — Output JSON + Decision Logic Engine (completato — 2026-05-17)

- `src/guazza/output.py`: `build_signals()`, `compute_coverage_30d()`, `write_location_json()`
- `src/guazza/jobs/predict.py`: job cron `predict` — modello → DB → DLE → JSON
- `src/guazza/storage.py`: `ensure_predictions_schema()`, `upsert_predictions()`, `backfill_prediction_obs()`
- `schema.sql`: tabella `predictions` v0.5 (3 target × 9 quantili/CI + `*_obs`)
- 18 test pytest, mypy e ruff OK
- **Pipeline end-to-end verificata**: un JSON per location in `data/output/`, 8 indicatori per location

**Signal bridge**: ML quantile → CDF inversa lineare per tmin/tmax/precip; NWP ensemble empirico per vento/umidità.
**`coverage_empirical_30d`**: tutti `null` al primo run — si popola dopo il primo mese operativo (via `backfill_prediction_obs`).

**`bisenzio` (chiuso — 2026-06-05)**: le soglie (3.5/5.5m per TOS01004791) erano già in
`indicators.yaml` e iniettate nel namespace di eval; l'indicatore funziona end-to-end
(casa_campi verde, `level_sir=0.7m`). Il fallback giallo scattava solo a `level_sir` assente.
Aggiunto meccanismo `requires` nel DLE → verdetto **grigio** ("Dato non disponibile") quando
il segnale chiave manca o è `None`, invece di un semaforo arbitrario (vedi CHANGELOG).

### Pre-Sprint 6 — completato (2026-05-18)

- **Soglie idrometriche `bisenzio`** (KI-012): soglie statiche in `config/indicators.yaml`
  (threshold_1=3.5m, threshold_2=5.5m, threshold_3=7.0m). `evaluate_indicator` inietta
  `cfg["thresholds"]` nel SignalBag prima dell'eval — indicatore sbloccato dal fallback giallo.

- **Predict job multi-giorno**: SQL con QUALIFY seleziona il miglior `lead_time_h` per ogni
  `(location_id, target_date)` futuro. JSON ha struttura `{location_id, generated_at,
  coverage_empirical_30d, days: [{target_date, lead_time_h, forecasts, indicators, hourly}]}`.

- **Profilo orario**: `compute_hourly_profile()` in `output.py`. Ensemble-mean NWP (latest
  ts_run per source) → temp rescalata su `[tmin_p50, tmax_p50]` ML; precip riscalata a
  sommare `precip_p50` ML; `precip_prob` = frazione modelli > 0.1mm/h. Ogni giorno nel JSON
  include `hourly: [{hour, temp_c, precip_mm, precip_prob}]` (24 elementi, null se no dati).

### Sprint 6 — Frontend (completato — 2026-05-18)

Stack: HTML + JS vanilla; **Tailwind CSS + DaisyUI v4** (CDN jsDelivr) + **Chart.js** (CDN); nginx statico; Cloudflare CDN/WAF.

#### Layout a 3 sezioni stile Foreca

**Sezione A — Condizioni attuali** (pannello principale)
- Temperatura grande + icona meteo derivata da realtime
- **Temperatura percepita** (`feels_like_c`, Steadman/BoM) e **punto di rugiada** (`dewpoint_c`, Magnus), calcolati server-side in `output.py`
- Mini-stats: Vento / Umidità / Precipitazione
- **Indicatori DLE di oggi** (`build_signals_today`): calcolati dalle osservazioni realtime
  (precip/vento/umidità deterministici 0/1; Tmin resta da ML perché la notte non è ancora finita)

**Sezione B — Previsioni giornaliere**
- Striscia card orizzontale scrollabile: oggi + D+1…D+7, icona meteo + Tmax/Tmin/precip + indicator dots
- Clic su card espande dettaglio: CI bar 80/90% per Tmin/Tmax/precip + 8 indicatori DLE + tabella confronto modelli NWP con **data ultimo run per modello** (`last_run`)

**Sezione C — Grafico unico multi-giorno**
- Chart.js mixed (line/bar): temperatura (arancio) + umidità (blu tratteggiato) + precipitazioni (barre, opacità proporzionale a `precip_prob`) + **vento km/h** (verde tratteggiato, asse destra)
- **Crosshair verticale** inline plugin
- Switch modello: Guazza ML ↔ 6 modelli NWP senza reload

#### Campi JSON aggiunti (output.py)

| Campo | Dove | Contenuto |
|---|---|---|
| `current.dewpoint_c` | payload root | Punto di rugiada calcolato (Magnus) |
| `current.feels_like_c` | payload root | Temperatura apparente (Steadman/BoM) |
| `current.wind_speed_ms` | payload root | Vento realtime SIR (spesso null su Netatmo base) |
| `days[].hourly[].wind_speed_ms` | per giorno | Vento NWP ensemble per ora (no rescaling) |
| `nwp_models_hourly[].data[].wind_speed_ms` | per modello | Vento per modello NWP |
| `days[].nwp_comparison[].last_run` | per giorno | Data ultimo run per modello (`strftime`) |

#### Fix post-deploy (2026-05-18)

- **Timestamp SIR +2h nel browser**: rimosso `|| '+00:00'` dal `strftime` in `get_current_conditions`. I timestamp SIR erano salvati come CEST naive; ora sono UTC naive (vedi standardizzazione 2026-05-30), strftime usa suffisso `Z`.
- **Lead time +48h invece di +24h per domani**: la QUALIFY in `predict.py` usava `lead_time_h DESC` per i giorni futuri, selezionando il forecast più vecchio. Corretto in `lead_time_h ASC` (forecast più recente) per tutti i giorni.

#### Fix e miglioramenti frontend (2026-05-18 — post Sprint 6)

- **Card border clipping**: il `ring-2` della card attiva veniva tagliato dall'`overflow-x-auto`. Fix: `p-1` sul wrapper scrollabile.
- **Grafico tendenza day-scoped**: `buildChartPoints` ora filtra al giorno selezionato. Asse X fisso 00-23h indipendentemente dalla disponibilità dati — se il modello non ha dati per quel giorno il grafico è vuoto ma l'asse resta.
- **Animazione transizione**: fade-in + slide-up 200ms (`@keyframes guazza-fade-in`) su ogni cambio location o giorno, riavviato via reflow forzato (`void el.offsetWidth`).

#### Indicatori realtime (`build_signals_today`)

Per `target_date == today`, `predict.py` chiama `build_signals_today` che sovrascrive i segnali probabilistici (precip/vento/umidità) con valori deterministici 0/1 dalle ultime osservazioni realtime. Temperatura minima resta da ML (la notte non è ancora completata).

🟡 **Punto aperto**: `current` è `null` finché non ci sono osservazioni SIR/Netatmo con `granularity='realtime'` nelle ultime 3h nel DB. In locale richiede `ingest realtime` manuale prima di `predict`; in produzione il cron ogni 30min lo mantiene fresco.

🟡 **Punto aperto**: wind in `current` è quasi sempre `null` — le stazioni Netatmo base non riportano il vento, e solo alcune SIR lo misurano in realtime. Candidato per Sprint 7 (raffinamenti).

### Configurazione e manutenzione (2026-05-18)

#### Quinta location: casa_nicco (Firenze Novoli)

- **`config/locations.yaml`**: nuova location `casa_nicco` (43.791, 11.219, 40m)
  - Primaria SIR: `TOS01001096` Firenze Università (~1km, ΔQ+44m) — più vicina, set sensori completo incluso anemo
  - Termo pesato su `TOS03001097` Orto Botanico (3.6km, ΔQ+8m) come prima scelta per temperatura
  - ARPAT: FI-MOSSE 0.50, FI-BOBOLI 0.30, FI-GRAMSCI 0.15, FI-LAVAGNINI 0.05
  - upstream_pluvio_stations: Vaiano, Fattoria Iavello, Santomato, Albano (NW/W)
  - Nessuna stazione idrometrica (Arno FI non in anagrafica SIR)
- **`config/stations.yaml`**: `used_by` aggiornato per TOS01001096, TOS03001097, TOS03001099; aggiunte 4 stazioni ARPAT Firenze
- **`frontend/app.js`**: aggiunta tab "Casa Nicco"

Post-config completato: `weights refresh`, `ingest forecasts`, `features build` e
`predict` eseguiti — casa_nicco genera regolarmente il proprio JSON di output.

#### Analisi ring coverage (2026-05-18)

Verificata copertura per tutte e 5 le location:

| Location | Ring1 (≤20km) | Ring2 (20-50km) | Ring3 (50-100km) |
|---|---|---|---|
| casa_campi | 15 stazioni | 14 | 1 |
| lavoro_cosimo | 12 | 17 | 1 |
| lavoro_madda | 18 | 9 | 3 |
| casa_cesto | 7 | 14 | 9 |
| casa_nicco | 11 | 18 | 1 |

Ring3 scarno per le location di pianura FI (1 stazione = Bagni di Lucca ~57km). Nessuna stazione SIR nel Mugello in anagrafica — gap geografico N/NE pre-esistente per tutte le location FI.

#### Fix SIR historical parallelismo (2026-05-18)

Il server `www.sir.toscana.it` serializza le connessioni lato server (~3s per request per IP): `ThreadPoolExecutor` non accelera il throughput. Rimosso `time.sleep(1.0)` da `_fetch_one` (era overhead puro su un processo già serializzato a livello di rete). `max_workers=3` mantenuto. Risparmio: ~28s su un backfill completo (28 combo).

### Qualità aria nel pannello realtime (2026-05-18)

- **Rimosso indicatore DLE `aria`**: la qualità aria non è un semaforo previsionale ma un
  dato osservativo. `config/indicators.yaml` passa da 9 a 8 indicatori.
- **`get_current_air_quality()`** in `output.py`: ultimi valori ARPAT per location.
  PM10/PM2.5/benzene da bollettini (`granularity='daily'`, finestra 2 giorni);
  NO2/O3/CO/SO2 da NRT (`granularity='hourly'`, finestra 3 ore). Campo top-level
  `air_quality` nel JSON, indipendente da `current`.
- **`renderAirQuality()`** in `app.js`: card per inquinante nel pannello realtime,
  colore da soglie ARPAT (`AQ_THRESHOLDS`), unità per-inquinante (mg/m³ per CO).
  Mostrate solo le voci effettivamente misurate dalle stazioni della location.
- **CO, benzene, SO2 aggiunti alla pipeline ARPAT**: erano nella risposta API ma
  scartati (`None` in VAR_MAP). 3 colonne nuove in `observations` (`co_mgm3`,
  `benzene_ugm3`, `so2_ugm3`), migrazione idempotente `_ensure_aq_columns()`.
- **Doc fix**: il comando è `predict` (modulo a comando singolo), non `predict run`.

### Cutover ARPAT → OpenAQ (2026-05-20, v0.6.2)

- **Sostituito completamente il fetcher qualità aria**: da scraping endpoint ARPAT
  (NRT orari + bollettini giornalieri) a OpenAQ v3 (aggregatore multi-provider che
  include ARPAT Toscana tra le sue fonti upstream).
- **Discovery dinamica per coordinate**: `_discover_openaq_stations(lat, lon, radius_m)`
  via `/v3/locations?coordinates=...`. Nessuna lista statica di station_id in config:
  ogni location interroga il raggio 15km e usa le stazioni trovate.
- **Solo realtime (`granularity='hourly'`)**: rimossi i bollettini giornalieri
  PM10/PM2.5. L'AQ non è feature di training e `get_current_air_quality()` usa
  finestra 3h — nessun backfill storico necessario (vedi KI-016).
- **Bug fix critici** (vedi CHANGELOG v0.6.2):
  - `/locations/{id}/latest` non include `parameter`: usare `sensorsId` per lookup
  - `station_id` include `location_id` per evitare PK collision tra location vicine
  - Timestamp convertiti a UTC naive (convenzione standard del DB — vedi standardizzazione 2026-05-30)
- **Frontend qualità aria sempre visibile**: tutti e 7 i parametri renderizzati
  anche quando null (`—`), griglia fissa a 7 colonne.

🟡 **Punto aperto — copertura ridotta per casa_cesto e casa_nicco** (KI-017):
- casa_nicco: FI-LAVAGNINI mancante su OpenAQ — impatto nullo (NO2 già coperto
  da 3 altre stazioni; FI-BASSI emerge come bonus con SO2 e PM2.5).
- casa_cesto: AR-ENELSB-SANGIOVANNI mancante — perdita di BENZENE e CO. Unica
  stazione OpenAQ nel raggio è FI-FIGLINE (3.7km) che aggiorna intermittentemente.
- **Decisione**: non reimplementare ARPAT NRT diretto. Costo (2 sorgenti AQ,
  API non documentata) sproporzionato vs beneficio (1 location, parametri non
  critici per indicatori DLE).

#### Fix grafico tendenza vuoto (2026-05-18)

- **Grafico tendenza vuoto all'apertura**: `buildChartPoints` per il giorno corrente
  usava `today_hourly` (solo ore future di oggi → vuoto a fine giornata). Ora il grafico
  usa sempre `days[].hourly` (profilo completo 24h) per il modello `guazza`.
- **Rimosso `today_hourly`** dal payload JSON e `get_today_hourly()` da `output.py`:
  non più consumato dal frontend dopo il fix sopra.
- **Crosshair su Edge**: `chart.tooltip._active` → optional chaining; `chart.tooltip`
  è `undefined` durante i primi `afterDraw` su Edge.

### Sprint 7 — Raffinamenti in locale
**Dipendenza**: nessuna — lavoro continuo prima del deploy

Iterazioni di affinamento su logiche e frontend per portare il sistema a uno
stato "production-ready" in locale prima di affrontare il deploy. Scope
aperto, definito turno per turno: bug fix, raffinamenti UX, micro-feature.
Criterio di uscita: tutto gira pulito in locale per ≥1 settimana senza
interventi.

#### Radar RainViewer (2026-05-22)

- **Sezione radar** inserita tra condizioni attuali e previsioni giornaliere
- **Leaflet 1.9.4** (CDN jsDelivr): mappa slippy con overlay tile RainViewer
- **RainViewer public API**: `radar.past` (ultimi 7 frame, ~60min osservati) + `radar.nowcast`
  (fino a 6 frame, +60min, solo se precipitazione attiva). Cache in-memory 5min.
- **Animazione**: opacity-swap su N tile layer a 2fps; pausa di default; `document.hidden` check
- **Basemap**: CARTO DarkMatter fisso (contrasto ottimale per colori radar)
- **Marcatore location**: `L.circleMarker` colore DaisyUI primary `oklch(0.6569 0.196 275.75)`
- **Timeline**: slider DaisyUI `range-primary`, posizione default = ultimo frame passato (ora corrente);
  etichette dinamiche proporzionali al numero di frame disponibili
- **Controlli**: zoom nativo Leaflet sostituito da `join-vertical` DaisyUI; play/pausa con SVG
  inline (niente Unicode/emoji); barra controlli orizzontale play + slider + ora
- **z-index**: pane/controlli Leaflet a `z-5` per non coprire header sticky (`z-10`)
- **Stile dark**: override CSS per Leaflet attribution e zoom bar
- **max zoom 7**: limite RainViewer (non Leaflet) — tile non disponibili a zoom 8+

#### Intraday correction D+0 (completato — 2026-05-31)

Correzione aritmetica D+0 ancorata alle osservazioni SIR realtime, senza nuovo modello.

- `get_intraday_observed()` in `output.py`: legge `MIN(temp_c)`, `MAX(temp_c)`,
  `MAX(precip_cumday_mm)` dalle obs realtime SIR del giorno corrente (solo `source='sir_toscana'`
  per evitare il bias da irraggiamento dei moduli Netatmo outdoor).
- `apply_intraday_correction()` in `output.py`: `precip_remaining = max(0, p50 - observed)`;
  CI scalato linearmente per `hours_remaining/24`. `tmin_corrected` attivo da ora ≥ 09:00 locale;
  `tmax_corrected` attivo da ora ≥ 14:00 locale. Soglia minima `_N_MIN_INTRADAY=3` osservazioni.
- Risultato salvato nel campo `intraday` di `days[0]` nel JSON di output.
- `jobs/predict.py`: chiama entrambe le funzioni solo per `target_date == date.today()`.
- `app.js` — helper `dayTemps()`: `intraday.tmin_corrected_c` / `tmax_corrected_c` /
  `precip_remaining_mm` hanno priorità sul forecast ML nella striscia e nel dettaglio.
- Test in `tests/test_output.py` (8 casi: precip exceeded, CI scaling, tmin/tmax before/after soglia).

#### Raffinamenti frontend (2026-05-20 → 2026-05-22)

- **Twemoji** (`twemoji@14.0.2`, jsDelivr): emoji Unicode renderizzate come SVG per
  consistenza cross-browser. Fix CSS `img.emoji` in `style.css`.
- **suncalc** (jsDelivr): sostituisce il calcolo NOAA manuale per alba/tramonto e
  fase lunare, calcolati client-side dalle coordinate location.
- **Alba e tramonto** nel pannello realtime: flex row con emoji + ora + tooltip DaisyUI.
- **Fase lunare**: 8 emoji lunari (🌑→🌘) con tooltip nome fase in italiano.
  Aurora civile e crepuscolo civile rimossi.
- **Tooltip CI bar**: hover sul track confidence interval mostra mediana e range
  80%/90% in testo leggibile.
- **Pressione atmosferica**: `pressure_hpa` (surface pressure Open-Meteo) esposta in
  `get_current_conditions()` e mostrata come 4a cella nella stats grid (grid-cols-4).

#### Redesign frontend v2 — CSS custom (2026-05-23 → 2026-05-28)

Riscrittura completa del frontend. Le descrizioni precedenti dello Sprint 6/7 che citano
Tailwind/DaisyUI riflettono lo stato pre-redesign.

- **Rimossi Tailwind CSS e DaisyUI**: il frontend ora usa **CSS custom** in `style.css`
  con classi prefissate `g-*`. Nessun framework CSS, nessun build step.
- **Palette "Carbone e Iride"**: 4 livelli di superficie carbone neutra (zero hue) +
  accento iris `#6B7FD4` riservato a stati vivi/attivi + 5 segnali semantici
  (verde/giallo/rosso DLE, warm/cold delta temperatura).
- **Tipografia**: Geist (display/titoli) + JetBrains Mono (tutti i valori numerici),
  caricati via Google Fonts CDN.
- **Nuovi documenti**: `DESIGN.md` (design system completo: colori, tipografia, componenti,
  regole) e `PRODUCT.md` (product brief: utenti, scopo, principi, anti-references).
- **Campo `mean`** (E[precip]) esposto nelle previsioni JSON (`output.py`).
- **Fix attribution RainViewer** riposizionata.

### Standardizzazione timestamp UTC (completato — 2026-05-30)

**Convenzione**: tutte le osservazioni nel DB (`observations`) sono **UTC naive**.
- SIR realtime/bulk: era CET naive (UTC+1 fisso) → ora UTC naive (`-1h` applicato)
- ARPAT NRT (hourly): era locale (CET/CEST) → ora UTC naive; vecchi record eliminati, re-ingest al prossimo cron
- Netatmo: era già UTC naive (driver DuckDB strippava TZ da aware UTC) → invariato
- SIR daily / ARPAT daily: etichette di giorno (mezzanotte naive), **non convertite** per convenzione — non sono istanti
- `forecasts`: rimane UTC-aware (modelli NWP ragionano in UTC)

**Codice**: `_parse_sir_realtime_ts`, `_parse_sir_bulk_meta_ts` in `fetchers.py` ora convertono CET→UTC.
ARPAT NRT (`_parse_arpat_nrt`) usa `_ITALY_TZ.astimezone(UTC)`.
`_CET` e `_ITALY_TZ` ora a livello modulo (prima di tutte le funzioni SIR/ARPAT).

**output.py**: `strftime` usa `%Y-%m-%dT%H:%M:%SZ` con suffisso `Z` per `current.ts`, `ts_valid` fallback NWP, `last_run`.
Il frontend interpreta questi timestamp come UTC e li converte correttamente in ora locale.

**Bug latente risolto**: `NOW()` in DuckDB è UTC; confrontarlo con timestamp naive CET causava finestre temporali sbagliate di 1-2h in estate (record validi esclusi o record scaduti inclusi in `get_current_conditions`, `get_current_air_quality`).

### Fix timestamp Netatmo UTC + ts_sir/ts_netatmo nell'hero (2026-05-31)

L'hero mostrava "dati SIR" 2h avanti (es. 12:44 invece di 10:44). Doppia causa.

- **Bug Netatmo (`fetchers.py` `_measure_ts`)**: ritornava un datetime *aware UTC*;
  all'insert nella colonna `TIMESTAMP` naive, DuckDB (session TZ `Europe/Rome`) lo
  riconvertiva in locale strisciandolo → ogni osservazione Netatmo salvata +2h. Fix:
  `…replace(tzinfo=None)` (UTC naive, come SIR/ARPAT). La nota precedente "Netatmo già
  UTC naive" era valida solo con session TZ UTC. Le righe vecchie sbagliate escono dalla
  finestra 3h entro poche ore (Netatmo non è usato per il training) → nessuna pulizia.
- **`current.ts` (`output.py` `get_current_conditions`)**: il blend usava `MAX(ts)` su
  SIR+Netatmo, pescando il +2h di Netatmo sotto l'etichetta "SIR". Ora il CTE tagga la
  sorgente e il SELECT espone `ts_sir` = `MIN(ts)` sulle stazioni SIR (freshness onesta,
  alcune aggiornano ogni 10', altre 15') e `ts_netatmo` = `MAX(ts)`. `ts` generico
  mantenuto per i consumer esistenti. Entrambi `null` su fallback NWP.
- **Frontend (`app.js`)**: l'hero mostra due etichette distinte, `dati SIR <ora>` e
  `dati Netatmo <ora>`, ciascuna omessa se la sorgente non contribuisce.
- Contract aggiornato (`current.ts_sir`, `current.ts_netatmo`). 280+ test verdi.

### Profilo orario ML su asse Europe/Rome (2026-05-31)

Il profilo orario del modello `guazza` ragionava in UTC mentre il grafico frontend usa
l'ora locale: le ore di bordo giornata risultavano disallineate. Conversione esplicita
a `Europe/Rome`.

- **`output.py`** — `compute_hourly_profile`: forecast NWP convertiti `UTC → Europe/Rome`
  (`AT TIME ZONE`), `HOUR(local_ts)` e filtro giorno in ora locale, sia per il profilo
  che per la moda dei `weather_code`. `get_nwp_models_hourly`: margine `-3h` sotto
  mezzanotte UTC per coprire le ore 00-01 locali (= 22-23Z del giorno prima).
- **`get_intraday_observed`** ristretto a `source='sir_toscana'`: i moduli Netatmo
  outdoor al sole hanno bias da irraggiamento (fino a +8°C) che falserebbe il MAX usato
  per ancorare tmax.
- **`app.js`** — helper `dayTemps()`: l'intraday correction D+0
  (`tmin/tmax_corrected_c`, `precip_remaining_mm`) ha priorità sul forecast ML, così
  striscia e dettaglio mostrano lo stesso numero (eliminata la duplicazione di logica
  intraday in `renderDayDetail`). `buildChartPoints`/`buildWeeklyPoints`: `h.hour` è ora
  locale → costruttore `new Date(...)` locale invece di `Date.UTC`.
- 280 test verdi, ruff + mypy OK.

### Rifinitura frontend — critique + audit (2026-05-31)

Pass di review guidata (impeccable critique → 30→33/40; audit tecnico → 17/20) con
remediation. Solo frontend, nessun tocco al contract JSON.

- **Palette grafici → token**: `--chart-temp/precip/wind/grid/axis` in `:root` come unica
  fonte (legenda/tooltip via `var()`, canvas via `getComputedStyle`). Rimossi il secondo
  blu `#2563EB` e lo slate degli assi; documentato in `DESIGN.md §Chart series`.
- **A11y / responsive**: scala font px → `rem` (resize testo, WCAG 1.4.4); `<h1>` sul
  brand; `aria-label` sui canvas; `aria-hidden` sulle icone decorative; `aria-current`
  sul tab attivo; touch target model-switch/pill a 44px su pointer coarse; `--text-3` a
  0.55 per il contrasto.
- **UX / pulizia**: griglie indicatori hero/dettaglio differenziate ("Oggi" / "Giorno
  selezionato"); copy coverage e label model-switch chiarite; rimosso CSS morto; blur
  header sticky a 12px.
- 🟡 Lasciati di proposito: transition su `width`/`left` (guadagno nullo, rischio
  regressione) e supporto `forced-colors`/`prefers-contrast` (non testabile alla cieca).

### Baseline backtest D+0 — de-risking della tesi (2026-05-29)

`analysis/baseline_backtest.py` (read-only, esplorativo). Conferma con i dati che il bias
di microclima esiste, è sistematico e correggibile **già a D+0** (lead 0-5h, l'orizzonte
più accurato → floor di skill). Metodo: forecast orario aggregato a daily sul giorno
Europe/Rome, ground truth = stazione SIR **primaria**, debias mensile appreso su anni
≤2024 e applicato al 2025 (split disgiunto, no leakage).

**Risultati chiave (tmin/tmax MAE grezzo→debiasato, test 2025):**
- `casa_cesto` (fondovalle): bias tmin **positivo su tutti i 6 modelli** (+0.5…+2.1°C) →
  i NWP non vedono la conca fredda, sovrastimano la minima. Microclima da manuale.
- `casa_nicco` (Firenze) arome tmin: bias +2.14°C → MAE 2.17→0.87 (+60%).
- `lavoro_madda` ecmwf tmin: bias −2.49°C → 2.52→1.12 (+56%).
- Il bias è **model-specific** (nessun NWP universalmente migliore) → conferma D-005.

**Riconciliazione baseline skill (chiuso — 2026-06-05)**: i due numeri non erano in
conflitto, misurano l'errore NWP contro ground truth diversi. Il backtest 0.75°C = NWP
**grezzo** vs stazione **primaria** (no ML); il +25.6% = skill **modello ML** vs NWP-mean
sul target **pesato**. Fattore dominante = ground truth (blend pesato vs singolo gauge,
gap fino a 2.14°C); aggregazione UTC vs Rome trascurabile (~0.01°C, ipotesi scartata).
Baseline del case study fissato sul target pesato; skill ricomputato a 6 modelli:
**+32% tmin / +43% tmax**. Dettaglio in D-016.

🟡 **Punto aperto — backfill multi-lead per D+1…D+7** (estende il 🟡 di Sprint 3, lead_time_h):
il backtest multi-giorno non è eseguibile finché lo storico contiene solo lead 0-5h. Due
strade: (a) ri-ingestare l'orizzonte completo per run dalla Open-Meteo Historical Forecast
API se l'API lo consente, (b) accumularlo dal deploy in poi (Sprint 8). Gate della tesi
completa sui giorni a venire.

### Sprint 8 — Deploy nel homelab (Dell Optiplex 3050 / Proxmox + Cloudflare Tunnel)
**Dipendenza**: Sprint 7 chiuso, sistema stabile in locale

- Host Proxmox sul 3050; Guazza come tenant (LXC con cron **oppure** namespace k8s)
- **Cloudflare Tunnel** (`cloudflared`): espone nginx su guazza.it senza IP pubblico né port forwarding; SSL terminato da Cloudflare
- Backfill storico (`historical`) per caricare SIR + Open-Meteo 2022→oggi
- Scheduling dei 4 job ingestion + `qc run` + `predict` (crontab o `CronJob` k8s con `concurrencyPolicy: Forbid`)
- Configurazione `.env` produzione (Netatmo, Healthchecks.io), `load_dotenv` per lettura DB_PATH e HEALTHCHECKS_URL
- **Backup Cloudflare R2**: job periodico per backup `.duckdb` + Parquet su Cloudflare R2 (10GB free tier, egress gratis) via `rclone` o `boto3`
- **CI**: GitHub Actions pubblica (test/lint/mypy, clean-room + badge). **CD**: pull-based nel homelab (DB DuckDB single-writer → PVC `ReadWriteOnce` su storage local-path se k8s)

### Sprint 9 — Model monitoring + nowcasting
**Dipendenza**: Deploy nel homelab completato (Sprint 8)

- Job cron che calcola `coverage_empirical_30d` rolling e la confronta con target (80% per CI80, 90% per CI90)
- Alert se coverage scende sotto soglia: log `ERROR` + ping `Healthchecks.io` fail
- Requisito obbligatorio D-004

#### Nowcasting orario — da pianificare (opzione C)

Predizione oraria 0-6h con aggiornamento ogni 15-30 min. Architetturalmente separato dal modello day-ahead:

- **Feature set diverso**: osservazioni SIR realtime correnti + trend ultime 3h + NWP più recente (run 0-6h)
- **Target**: precip_mm, temp_c orarie per le prossime 1-6h (orizzonte fisso, no lead_time_h variabile)
- **Modello separato**: training set su coppie (obs_t, features_t-1..t-3) → obs_t+1..t+6
- **Cadenza cron**: ogni 15-30 min (subito dopo `realtime` ingest)
- **Output JSON**: campo `nowcast` nell'output per location, striscia oraria 0-6h
- **Dipendenza dati**: almeno 6-12 mesi di `realtime` in produzione per training set sufficiente → non prima di Sprint 11+

### Sprint 10 — Calibrazione soglie DLE post-deploy
**Dipendenza**: 30-60 giorni di operatività in produzione (Sprint 8+9)

- Analisi log `indicator_log` in DuckDB dopo 30-60 giorni di produzione
- Validare e ritunare soglie in `config/indicators.yaml` (attualmente "BEST-GUESS iniziali")
- Documentare soglie calibrate con motivazione in `docs/decisions.md`

### Sprint 11 — Case study / pubblicazione
**Dipendenza**: sistema stabile in produzione con dati sufficienti (Sprint 8-10)

- Raccolta risultati: figure CRPS, coverage, skill score vs NWP grezzo
- Pulizia repo per release pubblica (rimuovere credenziali, aggiungere LICENSE, README pubblico)
- Documentazione replica: come rieseguire l'esperimento
- Scrittura articolo LinkedIn/Medium
