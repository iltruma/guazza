# Changelog

Tutte le modifiche rilevanti al progetto sono documentate qui.
Formato: [Keep a Changelog](https://keepachangelog.com/it/1.0.0/).
Versioning: major per sprint, minor per milestone interne.

---

## [Unreleased]

## [0.11.2] - 2026-06-27

Patch release: due bug fix su `_apply_cqr` e `walk_forward_cv` scoperti in
locale. CI GitHub Actions falliva su `test_predict_ci_ordering` dopo v0.11.1.

### Fixed
- **Nested CI in `_apply_cqr`**: per distribuzioni degenerate (precip_mm
  con cal set zero-inflated) il CQR naturale produce `q_hat_90 < q_hat_80`,
  causando `ci80_hi > ci90_hi` e violando la proprietà teorica del CI
  nested. Fix: dopo il calcolo dei bound, forza
  `ci90_lo = min(ci90_lo, ci80_lo)` e `ci90_hi = max(ci90_hi, ci80_hi)`.
  Standard in letteratura CQR (Romano 2019), non un hack.
- **CQR per-riga in `walk_forward_cv`**: la correzione CQR era applicata
  a bucket interi invece che alla riga singola. Risultato: i fold
  recenti (lead lunghi) usavano la correzione del fold giovane (lead
  0-6h), producendo metriche di calibrazione fuorvianti.
  Ora `_lead_time_bucket(int(lead_h))` viene applicata per-riga prima
  della correzione. Aggiunto anche breakdown per `lead_bucket` nel
  job `train eval`.

### Tests
- `test_apply_cqr_enforces_nested_ci` (nuovo): verifica esplicitamente
  il caso patologico (q_hat_80=0.32, q_hat_90=0.14) senza enforcement.
- `test_load_artifacts_roundtrip` esteso: confronta `feature_cols` e
  `cqr.keys` tra trained e loaded, verifica che `predict()` sui Booster
  ricostruiti da `artifacts.json` dia la stessa mediana del modello
  in memoria.

## [0.11.1] - 2026-06-27

Rimozione di GFS dal setup: NWP con ~6.7% record orari con `temp_c`
valorizzato, escluso dal multilead (`previous_dayN` non archiviato),
sostanzialmente inutile per il training. Soluzione: rimozione completa
invece di fixare il fetcher (costo/beneficio sfavorevole).

### Removed
- **GFS rimosso dal setup** (KI-025 risolto): 6 → 5 NWP
  (ECMWF IFS, ICON-EU, ICON-D2, AROME France, ARPAE ICON-2I).
  - `NWP_MODEL_PREFIXES` in `features.py`: 5 modelli
  - `OM_MODELS` in `fetch_openmeteo.py`: il fetcher live non scarica più GFS
  - `NWP_SOURCES` / `NWP_LABELS` in `affidabilita.js` + `output.py`:
    backtest grafico e model switch senza GFS
  - `_OM_PREVIOUS_DAY_MAX` in `fetch_openmeteo.py`
  - `models_available` in `config/sources.yaml`
- Le 246k righe GFS già presenti in `forecasts` restano nel DB (dati
  morti ma innocui, lasciati per non rompere audit).

### Changed
- `features.py`: pivot e ensemble mean/spread ricalcolati sui 5 NWP
  (righe SQL ripulite). `NWP_FEATURE_COLS` auto-derivato, 25 feature NWP
  invece di 30.

### Note operative
- **Azione richiesta**: `features build` (ricrea `features_daily` senza
  colonne `gfs_*`) + `train run` (retrain con 25 feature NWP).

## [0.11.0] - 2026-06-27

Sprint 11 (parte 1) — Skill history time series + pagina "Come ha performato nel tempo".
Risposta al feedback "vorrei vedere se i modelli ci hanno preso nel passato" invece del
solo MAE per lead aggregato. Approccio incrementale: append giornaliero, dump JSON,
frontend con filtri finestra.

### Added
- **Tabella DuckDB `skill_history_daily`** in `src/guazza/schema.sql`: PK composta
  `(location_id, target_date, source, variable, lead_h)` + indice di ricerca.
  Append idempotente via `INSERT ... ON CONFLICT DO UPDATE`.
- **Job `src/guazza/jobs/skill_history.py`** (typer single-command) con due comandi:
  - `append [--day YYYY-MM-DD | --days N]` (default: ieri): calcola forecast a D-1
    (lead 24h) per ogni location × source (Guazza + 6 NWP) × variable (tmin, tmax,
    precip) e fa upsert. Da `predictions` per Guazza, da aggregazione daily di
    `forecasts` per NWP, da `obs_weighted_daily` per actual. Riusa `DuckDBClient`
    (init_schema idempotente) e `job_run` (ping Healthchecks, log JSON, exit code).
  - `dump [--output PATH]` (default `frontend/data/skill_history.json`): aggrega
    la tabella in un JSON time series. Una entry per (location, variable) con date
    allineate, valori per ogni source, finestra `min_date` → `max_date`. Scrittura
    atomica via tmp file.
- **Pagina `affidabilita.html`** (estesa): aggiunta sezione "Come ha performato
  nel tempo" sotto la curva MAE per lead. Due canvas affiancati (T max / T min),
  filtri 7gg / 30gg / Totale, 8 linee per grafico (actual nera + Guazza accent
  + 5-6 NWP grigi tratteggiati). NWP con tutti valori null nella finestra sono
  nascosti automaticamente (onestà su modelli "morti" come GFS).
- **`frontend/data/skill_history.json`**: nuovo file rigenerabile esposto al
  frontend via nginx (path `/data/skill_history.json`).
- **9 test pytest** in `tests/test_skill_history.py`: `_collect_rows` con
  FakeCon (location, obs vuote, var NULL, NWP mancanti), `_dump_payload`
  (struttura + tabella vuota), `_atomic_write_json` (replace + parent dirs),
  smoke test dei comandi typer.

### Frontend (`affidabilita.html` + `affidabilita.js`)
- Link nell'header dell'SPA: pill "Affidabilità" accanto a "Previsioni" (riusa
  `.g-tab` esistente, niente CSS nuovo oltre a 2 righe `.g-header__pages`).
- Sezione history: `.g-skill__hist` (2-col grid responsive), riusa `.g-card`,
  `.g-skill__seg` (segmented control 7gg/30gg/Totale), `.g-chart-legend`.
- Caricamento parallelo di `skill.json` + `skill_history.json` in `boot()`;
  history è best-effort (la pagina resta valida anche senza).
- Tooltip history mostra solo Actual e Guazza (i 6 NWP sono "rumore visivo" —
  il pattern tratteggiato rende l'idea dell'incertezza senza intasare il tooltip).
- `dayTemps()` e tutto il resto dello SPA intoccati (modifica isolata a
  `affidabilita.*`).

### Known
- **GFS ha record orari senza `temp_c` valorizzato** (~6.7% del totale). Il
  backtest GFS è quindi vuoto. Causa probabile: l'API Open-Meteo per GFS ha
  cambiato parametri o il fetcher non li estrae correttamente. Da investigare
  in `fetch_openmeteo.py` (KI-025). Nel frattempo il backtest funziona con
  5 NWP (ECMWF IFS, ICON-EU, ICON-D2, AROME France, ARPAE ICON-2I).

### Note operative
- **Schedule k8s proposta** per il job `skill_history append`:
  `15 6 * * *` UTC (15 min dopo `daily` ingest delle 06:00, così le obs di
  ieri sono nel DB). Il `dump` può essere hookato a `predict` o a un cron
  separato (es. `30 6 * * *`).
- **Backfill iniziale**: il comando `append --days N` permette di popolare la
  tabella con N giorni indietro. Eseguito `append --days 30` in locale al
  deploy: 588 righe in 1s.

## [0.10.0] - 2026-06-27

Sprint 9 — Adaptive Conformal Inference + monitor copertura. Risposta al
drift di calibrazione CQR già in atto sui fold recenti del walk-forward CV
(vedi KI-023: `coverage_80` post-drift = 0.688/0.699 vs target 0.80).

### Added
- **`AdaptiveConformalizer`** in `src/guazza/models.py`: classe ACI (Gibbs & Candès 2021)
  con `update(covered) → alpha_t` e `correct(offset)` per scalare il CI. Garantisce
  copertura long-run marginal anche sotto distribution shift. Mapping alpha→CI
  lineare sufficiente per spike; in Sprint 11+ si raffina con quantile function
  esplicita.
- **Persistenza ACI state** in DuckDB: `aci_state(target, lead_bucket, alpha_t_80,
  alpha_t_90, n_updates, err_sum_80, err_sum_90, updated_at)`. API: `ensure_aci_schema`,
  `get_aci_state`, `upsert_aci_state` in `storage.py`. Sopravvive ai restart del job.
- **Integrazione predict**: `jobs/predict.py` aggiorna ACI su TUTTE le prediction
  passate con actual valorizzato (via `backfill_prediction_obs`) e applica la
  correzione ACI ai bound CI delle prediction future via `apply_aci_correction`.
  Drop-in trasparente: cold start (n_updates < 30) usa CQR statico.
- **`jobs/monitor.py`**: nuovo job CLI per il monitoraggio copertura. Calcola
  `coverage_30d` per (target, lead_bucket) aggregato su tutte le location, logga
  WARN/INFO, pinga Healthchecks `/fail` se drift > 5pp dal target. Schedule:
  `5 9 * * *` UTC (dopo daily ingest che backfilla obs).
- **Cold start**: `ACI_COLD_START_N = 30` in `models.py`. Prime 30 obs per bucket
  usano CQR statico, poi ACI prende il sopravvento.
- **Cache ACI per bucket**: predict job carica `(aci_80, aci_90)` una volta per
  (target, lead_bucket) invece di una volta per riga (ottimizzazione batch).
- **Test integrazione**: 16 nuovi test (ACI round-trip DuckDB, apply_aci_correction
  cold/warm/overcoverage, get_aci_pair cold/warm, monitor coverage 30gg + alert
  drift, finestra 30gg). Totale: **334 test verdi**.

### Changed
- **Nessun cambiamento al contract JSON di output**: il JSON di predict ha lo
  stesso shape di prima. I bound CI passano attraverso `apply_aci_correction`
  (drop-in trasparente), ma il consumatore finale non vede differenze salvo che
  i CI bounds sono leggermente più larghi/stretti se ACI è warm.
- **Schedule cron k8s** (in `docs/status.md`): aggiunto `monitor 5 9 * * *`.
  Commentato `train run` settimanale (ACI corregge la confidenza ma non il
  modello — il modello resta addestrato sui dati storici finché non rifit).
- `deploy/crontab.template`: aggiunta riga monitor + commento esplicito sul perché
  train run è commentato.

### Notes
- **KI-024 (ex KI-023 — anomaly target spike, vedi status.md)**: rimane valido.
  Spike anomaly target 2026-06-27 ha mostrato +28/+44% MAE su tmin/tmax
  (rollback eseguito). Anomaly code tenuto come spike documentato, ANOMALY_TARGETS
  = () di default.
- **Non rompe retrocompat**: artifacts.json esistenti senza `anomaly_targets`
  caricati correttamente (default lista vuota).

## [0.9.0] - 2026-06-13

Rifattorizzazione trasversale su manutenibilità, sicurezza e performance. Nessun
cambiamento al contract JSON di output. **Richiede retrain** (`train run`): il formato
degli artefatti modello è cambiato (vedi Security).

### Added
- `src/guazza/_paths.py`: path di default centralizzati (DB_PATH, CONFIG_DIR, OUTPUT_DIR)
  letti dalle env in un unico punto, prima sparsi e ridefiniti in ~7 moduli.
- `src/guazza/jobs/_common.py`: helper condivisi dei job cron — `ping_healthchecks`,
  context manager `job_run()` (ping start/ok/fail + timing + `log_scrape` + exit 1 su
  eccezione, prima duplicato in 6 job), opzioni typer `--db`/`--config-dir`/`--output-dir`.
- `src/guazza/fetch_common.py`: costanti e helper HTTP condivisi dai fetcher (User-Agent,
  `is_retryable_http`, timezone CET/Italy).
- Vista DuckDB `obs_weighted_daily` in `schema.sql`: fonte unica della media pesata SIR
  daily per location, prima duplicata in 3 punti (features + 2 backfill `*_obs`).
- `models.predict_frame()`: predizione in batch per più righe (output identico a
  `predict()` riga-per-riga), usata dal job predict per evitare 15 chiamate-modello
  per giorno.

### Changed
- **Split di `fetchers.py`** (2022 righe) nei moduli per dominio `fetch_sir`,
  `fetch_openmeteo`, `fetch_netatmo`, `fetch_arpat` + `fetch_common`; `fetchers.py` resta
  la sola CLI. Nessun cambio di comportamento.
- `_log_scrape` → `log_scrape` in `_logging.py` (sede naturale del logging).
- Dedup: SIR bulk realtime ora table-driven (4 blocchi → 1 loop); batch Open-Meteo
  historical/multilead condividono `_chunk_date_range` + un runner comune; pivot NWP e
  `FEATURE_COLS` derivati da un'unica mappa `NWP_MODEL_PREFIXES` (impossibile divergere).
- Job predict: `init_schema()` idempotente all'avvio (garantisce la vista
  `obs_weighted_daily`); predizione in batch per location.
- SIR historical: loop sequenziale esplicito al posto di `ThreadPoolExecutor(max_workers=1)`
  (il server SIR serializza per IP — il pool non dava parallelismo reale).

### Security
- DLE: la valutazione delle condizioni YAML non usa più `eval()` ma un interprete su AST
  con whitelist di nodi (`indicators.py`). Rimuove il rischio di esecuzione arbitraria e
  corregge un bug latente sui segnali con `AND`/`OR` interni alla chiave (es. nebbia).
- Persistenza modelli: da `pickle` a manifest `artifacts.json` + model-string LightGBM
  `.txt` per quantile. Un artefatto manomesso non può più eseguire codice al load; i file
  sono ispezionabili. Il vecchio `artifacts.pkl` viene rifiutato con errore esplicito
  → **retrain necessario**.
- Scrittura atomica (tmp + `os.replace`) dei JSON di output e di `skill.json` (nginx non
  può più servire un file troncato durante il cron); refresh del token Netatmo nel `.env`
  ora atomico + `chmod 600`.

## [0.8.3] - 2026-06-05

### Added
- Backfill multi-lead D+1…D+7 (`ingest multilead` / `fetch_openmeteo_multilead_batch`):
  ricostruisce dallo storico cosa ogni modello prevedeva 1-7 giorni prima, via le variabili
  `<var>_previous_dayN` della Historical Forecast API. Mappa `previous_dayN` →
  `lead_time_h=24N`, `ts_run=mezzanotte(T−N)`, upsert in `forecasts`; la pipeline features
  aggrega senza modifiche. Abilita il backtest multi-giorno **senza deploy**. Orizzonte
  model-dependent (ECMWF D+7, ICON-EU D+4, ICON-2I D+2, ICON-D2/AROME D+1; GFS escluso —
  non archivia run precedenti). Vedi D-016 / punto aperto Sprint 7.
- `analysis/backtest_multilead.py`: backtest multi-lead D+0…D+7 (modello addestrato prima
  della finestra, valutato out-of-sample). Risultato: Guazza batte il NWP-mean a ogni lead;
  skill tmin vs gauge +13…+33% crescente col lead, tmax +5…+13% (D-016). Archivio
  `previous_dayN` disponibile da ~nov 2025 → backtest su ~7 mesi (inverno-primavera).

## [0.8.2] - 2026-06-05

### Fixed
- Target di training corrotto per le stazioni condivise (KI-022): `obs_weighted` in
  `features.py` joinava `observations` con `station_weights` anche su `location_id`,
  scartando i contributi delle stazioni che pesano su più location (le obs sono salvate
  sotto una sola `location_id` "home"). `lavoro_cosimo` aveva il target `tmin` **nullo al
  100%** (modello mai addestrato); `lavoro_madda` un bias di −2°C. Fix: join solo su
  `station_id`, `GROUP BY sw.location_id` (come `ring_precip_raw`). Dopo rebuild+retrain
  la copertura target sale a ~99% per tutte le location. Lo skill CV tmin scende da +32.5%
  a +15.6% (era gonfiato dal target corrotto); tmax resta +42.6%. Scoperto dal robustness
  check `analysis/skill_vs_primary.py` (D-016).

### Added
- `analysis/skill_vs_primary.py`: robustness check read-only che valuta lo skill ML vs
  NWP-mean contro la **stazione SIR primaria** (gauge indipendente), out-of-sample, oltre
  che contro il target pesato. Risultato: tmin +8%, tmax +26% vs gauge (D-016).

## [0.8.1] - 2026-06-05

### Added
- DLE: verdetto `grigio` (`rule_matched: "unknown"`) quando un segnale dichiarato in
  `requires` manca o è `None`. Sostituisce il fuorviante fallback giallo (falso allarme)
  e il falso "verde nella norma" che la regola dava interpretando `None` come 0.0.
  Applicato a `bisenzio` (`requires: ["level_sir"]`): senza livello idrometrico mostra
  "Dato non disponibile" invece di un semaforo arbitrario. Frontend: pill/dot grigi
  (`--grigio`), contract aggiornato.
- Sesta location `casa_cercina` (Sesto Fiorentino, versante S di Monte Morello,
  311m). Termo ancorato a Vaiano (TOS11000503, 322m, ΔQ+11m): unica SIR alla quota
  di Cercina. Le NWP Open-Meteo sono già downscalate a 311m, quindi un target SIR di
  pianura (ΔQ -200/-280m) introdurrebbe un train/serve skew in quota (vedi D-018).
  Pluvio/anemo su vicine di pianura, nessuna idrometrica, ARPAT FI-MOSSE/FI-LAVAGNINI.
- Accumulo Netatmo daily forward-looking (`netatmo_daily.py`, job
  `guazza.jobs.netatmo_daily`): aggrega il realtime Netatmo in righe
  `granularity='daily'` (tmin/tmax/humidity; precip esclusa per overlap `rain_1h`)
  sul giorno locale Europe/Rome. Non entra nel training (`features.py` resta
  `source='sir_toscana'`); costruisce lo storico per caratterizzare in Sprint 9+
  l'offset SIR-pianura ↔ microclima (vedi D-018). Agganciato al job `daily`.

### Changed
- Frontend: palette dei grafici spostata su token `--chart-*` in `:root` come unica
  fonte (legenda/tooltip via `var()`, canvas via `getComputedStyle`); eliminato il
  secondo blu `#2563EB` e lo slate degli assi. Documentata in `DESIGN.md §Chart series`.
- Frontend: scala tipografica migrata da px a `rem` (rispetto del resize testo del
  browser, WCAG 1.4.4).

### Fixed
- Qualità aria: `get_current_air_quality` risolve le stazioni via JOIN
  `station_weights` (source='arpat') invece di `observations.location_id`. La PK di
  `observations` non include `location_id`, quindi una stazione ARPAT condivisa tra
  location porta un solo tag arbitrario: la query precedente perdeva l'AQ per le
  location che condividono tutte le stazioni (es. casa_cercina ↔ casa_nicco) ed era
  una race. Ora media pesata per stazione (peso dal config). `weights refresh`
  popola anche i pesi ARPAT.
- Frontend a11y: aggiunto `<h1>` sul brand, `aria-label` sui canvas, `aria-hidden` sulle
  icone decorative, `aria-current` sul tab attivo; touch target di model-switch e pill a
  44px su pointer coarse; contrasto `--text-3` alzato a 0.55.
- Frontend UX: griglie indicatori hero/dettaglio differenziate ("Oggi" / "Giorno
  selezionato"); copy coverage e label model-switch chiarite (tooltip esteso); rimosso
  CSS morto (`live-badge`, keyframe `live-pulse`); blur header sticky ridotto a 12px.

## [0.8.0] — 2026-05-31

### Added
- Blend realtime: peso aggregato Netatmo sublineare `1/√N` (`output.py`). N sensori
  consumer indipendenti riducono la varianza come √N, non N: dividere ogni peso per √N
  evita che la mera densità di moduli (es. ~130 a Firenze urbana) sommerga le stazioni
  SIR validate per conteggio.
- Grafici daily/weekly: riga di icone meteo Meteocons sotto l'asse X, allineate ai pixel
  dei tick (daily a ogni tick orario, weekly ogni 6h) con varianti giorno/notte.
- Grafici daily/weekly scrollabili in orizzontale su mobile (`min-width` + `overflow-x`)
  per leggere il dettaglio orario senza compressione.
- Day strip e header dettaglio: data estesa (es. "6 giugno") sotto il nome del giorno
  (`fmtDayNumber`).

### Changed
- Grafici: assi Y con range **identico per tutti i modelli** (unione dei dati) così il
  confronto tra modelli resta leggibile cambiando modello; step "nice" per tick
  equispaziati; asse precipitazioni dedicato e nascosto; asse X `offset:false` +
  `bounds:'data'` per riempire la larghezza senza margini vuoti.
- Tabella `nwp-list`: rimossa la colonna Run; il run di Guazza ML è mostrato inline nel
  nome come per gli altri modelli.
- Profilo orario ML allineato all'asse Europe/Rome; timestamp Netatmo in UTC con
  `ts_sir`/`ts_netatmo` separati nell'hero.

### Fixed
- Blend realtime: esclusi i moduli Netatmo con `qc_pass=False` (il flag QC calcolato in
  ingestion non veniva applicato in `get_current_conditions`).
- Tooltip dei grafici posizionato sopra il puntino della curva senza coprirlo.
- Tabella daily: colonne `tmax`/`tmin` invertite.

### Removed
- Serie umidità dai grafici daily/weekly (dataset, asse `yHum`, voce di legenda, riga
  tooltip): la temperatura usa l'asse sinistro per intero.

## [0.7.2] — 2026-05-29

### Added
- Icone meteo animate Meteocons (`@meteocons/svg@0.1.0`, MIT) servite via CDN jsDelivr
  per le condizioni `weather_code` WMO (hero, striscia giorni, dettaglio). Renderizzate
  come `<img class="g-wicon">` SVG con animazione SMIL, non parsate da twemoji. Nuovo
  helper `weatherIconHtml`, campo `iconName` (con varianti day/night) in `wmoCondition`,
  classi CSS `.g-wicon*`. Fallback `onerror` all'emoji nativa se il CDN non risponde.

### Changed
- Le icone delle condizioni meteo passano da emoji twemoji a SVG animate Meteocons.
  Tutte le altre icone (indicatori, mini-stats, header, luna, alba/tramonto) restano twemoji.

## [0.7.1] — 2026-05-29

### Added
- `weather_code` WMO da Open-Meteo: ingestito come `INTEGER` per ogni ora e modello NWP
  (`fetchers.py`, `schema.sql`), migrazione idempotente `_ensure_forecast_columns`
  (`storage.py`). Nuovo helper `get_daily_weather_code` (moda 24h × N modelli, tie-break
  per severità WMO) e `_modal_weather_code` in `output.py`. Campo `weather_code: int | null`
  aggiunto a `current`, `days[]`, `nwp_models_hourly[].data[]` e `hourly[]` nel JSON di output.
  Frontend: `wmoCondition(code, isNight)` sostituisce l'euristica `weatherCondition`; hero
  usa `current.weather_code` con fallback su `todayDay.weather_code` e override pioggia
  se `current.precip_mm > 0.2`.
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
