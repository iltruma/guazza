# Changelog

Tutte le modifiche rilevanti al progetto sono documentate qui.
Formato: [Keep a Changelog](https://keepachangelog.com/it/1.0.0/).
Versioning: major per sprint, minor per milestone interne.

---

## [Unreleased]

### Added
- **Feature engineering** (Sprint 3): `features.py` costruisce `features_daily`,
  training set materializzato in DuckDB — NWP 6 modelli pivot wide, ensemble
  mean/spread, obs SIR pesate lookahead-safe, climatologia mensile, target
  ground truth. CLI `jobs/features.py` (`build`/`info`)
- **Backfill SIR pre-2022** (Sprint 2b): `fetch_sir_historical` esteso oltre il
  2022; verificato limite archivio per stazione
- **Quality control** (Sprint 2c): tabella `quality_flags`, `qc.py` +
  `jobs/qc.py` (`run`/`report`); eliminati dati pre-2004 (pluviometro SIR
  non disponibile prima)
- **QC ARPAT flags**: `range_pm10_high`, `range_pm25_high`, `range_no2_high`, `range_o3_high`
- **QC realtime precip**: `range_precip_high` esteso a `granularity='realtime'`
- **Transazione**: `compute_quality_flags` in `BEGIN/COMMIT/ROLLBACK`
- **Breakdown dict**: `compute_quality_flags` restituisce `{"total": N, "spike_tmin": M, ...}`
- **`--dry-run`**: aggiunto a `jobs/qc.py run`
- **Breakdown log**: il job logga il dettaglio per tipo flag
- **Open-Meteo coordinate batching** (D-011): tutte le location in una chiamata per modello
- **Open-Meteo temporal chunking** (D-012): backfill storico frazionato in chunk (180gg / 90gg HR)

### Changed
- **Modelli NWP** (D-013): da 5 a 6 modelli — `ecmwf_ifs025` (25km) sostituito da
  `ecmwf_ifs` HRES (9km), aggiunto `icon_d2` (2.2km)
- `_fetch_om_json_historical` separato dal forecast live: timeout 90s, 5 retry,
  `Retry-After` rispettato sul 429

### Fixed
- `jobs/ingest.py`: `end_date` Open-Meteo cappato a oggi-2gg (limite archivio)
- `fetchers.py`: 429 reso retryable, skip retry su 4xx permanenti

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

[Unreleased]: https://github.com/cosimo/guazza/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cosimo/guazza/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/cosimo/guazza/releases/tag/v0.0.1
