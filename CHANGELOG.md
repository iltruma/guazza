# Changelog

Tutte le modifiche rilevanti al progetto sono documentate qui.
Formato: [Keep a Changelog](https://keepachangelog.com/it/1.0.0/).
Versioning: major per sprint, minor per milestone interne.

---

## [Unreleased]

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

[Unreleased]: https://github.com/cosimo/guazza/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/cosimo/guazza/compare/v0.1.0...v0.4.0
[0.1.0]: https://github.com/cosimo/guazza/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/cosimo/guazza/releases/tag/v0.0.1
