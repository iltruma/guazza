# Guazza

> *Guazza* (dal latino *aquatia*): rugiada pesante che si forma nelle conche toscane durante notti serene e umide. Il nome rimanda al fenomeno microclimatico che i modelli standard non catturano.

Previsioni meteo iper-locali per 4 microclimi toscani. Sistema operativo personale + case study tecnico pubblicabile.

**Tesi**: i modelli numerici pubblici (ECMWF, ICON-EU, app commerciali) sbagliano sistematicamente sui microclimi specifici generati da orografia, fondi valle e isole di calore. Questo progetto lo dimostra empiricamente e produce un sistema che fa misurabilmente meglio.

**Costo infrastruttura**: ~€7/mese (VPS Hetzner CX22 + dominio).

---

## Setup locale

### Prerequisiti

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

### Installazione

```bash
git clone https://github.com/<tuo-user>/guazza.git
cd guazza
uv sync
cp .env.example .env
# Compila .env con le credenziali Netatmo e Healthchecks.io
```

### Verifica installazione

```bash
uv run ruff check src/ && uv run mypy src/
uv run pytest tests/ -v
DB_PATH=/tmp/guazza_test.duckdb uv run python -m guazza.storage verify-schema
```

---

## Struttura del repo

```
guazza/
├── config/
│   ├── locations.yaml      # 4 location con stazioni SIR e upstream_pluvio_stations
│   ├── stations.yaml       # 34 stazioni SIR (21 operative + 13 upstream pluvio ring)
│   ├── indicators.yaml     # 9 indicatori DLE con soglie e costi asimmetrici
│   ├── arpat_levels.yaml   # Scale qualità aria D.Lgs.155/2010
│   └── sources.yaml        # Endpoint sorgenti dati e stato
├── src/guazza/
│   ├── schema.sql          # Schema DuckDB — unica source of truth
│   ├── storage.py          # DuckDBClient, upsert bulk Arrow, backfill_prediction_obs
│   ├── fetchers.py         # SIR storico/realtime, Netatmo, Open-Meteo (6 modelli), ARPAT
│   ├── weights.py          # Pesi stazione→location, ring upstream pluvio
│   ├── features.py         # build_features_daily() — 50 feature, tabella materializzata
│   ├── models.py           # LightGBM quantile + CQR, train_all(), predict()
│   ├── indicators.py       # Decision Logic Engine: evaluate_all(), log_results()
│   ├── output.py           # build_signals(), compute_coverage_30d(), write_location_json()
│   ├── qc.py               # Quality control osservazioni SIR + ARPAT
│   ├── _logging.py         # setup_logging() — TTY pretty / cron JSON strutturato
│   └── jobs/
│       ├── ingest.py       # Cron: historical / daily / realtime / forecasts
│       ├── features.py     # CLI: features build / info
│       ├── train.py        # One-shot: train run / train eval (walk-forward CV)
│       ├── predict.py      # Cron: predict run → DuckDB + DLE + JSON output
│       ├── qc.py           # Cron: qc run / qc report
│       └── backup.py       # Cron: backup DuckDB su Cloudflare R2 (Sprint 7)
├── data/
│   ├── guazza.duckdb       # Database analitico (non committato)
│   ├── models/             # Artefatti LightGBM pickle (non committati)
│   └── output/             # JSON per il frontend (non committati)
├── deploy/                 # nginx.conf, Caddyfile, crontab template
├── tests/
└── docs/
    ├── status.md           # Stato corrente — leggere a inizio sessione
    ├── decisions.md        # Decisioni architetturali motivate
    └── known_issues.md     # Problemi noti + workaround
```

---

## Roadmap

| Sprint | Obiettivo | Stato |
|---|---|---|
| Sprint 0 | Ricognizione sorgenti, config stazioni, struttura repo | ✅ Completato |
| Sprint 1 | Ingestion SIR + Netatmo + Open-Meteo + ARPAT, schema DuckDB, job cron | ✅ Completato |
| Sprint 2 | Backfill SIR pre-2022, quality control (SIR + ARPAT), flag qualità | ✅ Completato |
| Sprint 3 | Feature engineering, 50 feature, ring upstream pluvio | ✅ Completato |
| Sprint 4 | LightGBM quantile + CQR, skill +25% vs NWP su temperatura | ✅ Completato |
| Sprint 5 | Output JSON, Decision Logic Engine, indicatori operativi | ✅ Completato |
| Sprint 6 | Frontend HTML+JS vanilla | — |
| Sprint 7 | Deploy VPS, backup R2, crontab | — |
| Sprint 8 | Model monitoring, coverage alert | — |
| Sprint 9 | Calibrazione soglie DLE post-deploy | — |
| Sprint 10 | Case study / pubblicazione | — |

---

## Architettura

- **VPS**: Hetzner CX22 (2 vCPU, 4GB RAM, €3.79/mese)
- **Orchestrazione**: cron Linux (no Prefect, no Airflow)
- **Storage**: DuckDB (colonnare, file singolo) + R2 backup
- **ML**: LightGBM quantile regression + CQR calibration
- **Frontend**: HTML+JS vanilla (Sprint 6)
- **DNS/CDN/WAF**: Cloudflare (gratis)
- **Monitoring**: Healthchecks.io + UptimeRobot (free tier)

Vedi `docs/decisions.md` per motivazioni complete.

---

## Pipeline operativa

### Ingestion

```bash
# Backfill storico one-shot (SIR + Open-Meteo 2022→oggi)
uv run python -m guazza.jobs.ingest historical

# Delta giornaliero — schedulare a 06:00 UTC
uv run python -m guazza.jobs.ingest daily

# Realtime SIR + Netatmo — schedulare ogni 15-30 min
uv run python -m guazza.jobs.ingest realtime

# Forecast NWP — schedulare ogni 6h (02/08/14/20 UTC)
uv run python -m guazza.jobs.ingest forecasts
```

### Feature engineering + training

```bash
# Ricostruisce features_daily (da eseguire dopo ogni ingest)
uv run python -m guazza.jobs.features build

# Pesi stazioni + ring upstream pluvio
uv run python -m guazza.weights refresh

# Training LightGBM + CQR (one-shot o dopo backfill significativi)
uv run python -m guazza.jobs.train run --db data/guazza.duckdb --model-dir data/models

# Walk-forward CV con metriche
uv run python -m guazza.jobs.train eval --db data/guazza.duckdb
```

### Predizioni + indicatori

```bash
# Genera predizioni quantile + DLE + JSON (schedulare ogni 6h dopo il job forecasts)
uv run python -m guazza.jobs.predict run --db data/guazza.duckdb \
    --model-dir data/models --output-dir data/output
```

Output: `data/output/{location_id}.json` con CI80/CI90 per tmin/tmax/precip,
9 indicatori semaforo (panni, motorino, gelata, ...) e `coverage_empirical_30d`.

### Opzioni comuni

```bash
--dry-run    # Simula senza scrivere
--db PATH    # Path DuckDB (default prod: /var/lib/guazza/guazza.duckdb)
```

Variabile d'ambiente `HEALTHCHECKS_URL` per il ping dead-man switch.

---

## Come ricostruire da zero

```bash
# 1. Clone e setup
git clone https://github.com/<user>/guazza.git && cd guazza
uv sync && cp .env.example .env

# 2. Edita .env con credenziali reali (Netatmo, Healthchecks.io)

# 3. Inizializza schema DuckDB
DB_PATH=/var/lib/guazza/guazza.duckdb uv run python -m guazza.storage init-schema

# 4. Backfill storico (lento — SIR + Open-Meteo 2022→oggi)
uv run python -m guazza.jobs.ingest historical

# 5. Pesi stazioni + ring upstream
uv run python -m guazza.weights refresh

# 6. Feature engineering
uv run python -m guazza.jobs.features build

# 7. Training modello
uv run python -m guazza.jobs.train run

# 8. Prima previsione
uv run python -m guazza.jobs.predict run

# 9. Installa crontab sul VPS
crontab deploy/crontab.template
```

---

## Sviluppo

```bash
uv run pytest tests/ --cov=guazza --cov-report=term-missing
uv run ruff check src/ tests/
uv run mypy src/
```

---

## Sorgenti dati

| Sorgente | Uso | Accesso |
|---|---|---|
| Open-Meteo Forecast + Historical | 6 modelli NWP (ECMWF, ICON-EU, ICON-D2, GFS, AROME, ICON-2I) | API pubblica, no key |
| SIR Toscana | Ground truth osservazioni validate, 34 stazioni | Open Data |
| Netatmo | Osservazioni iperlocali real-time | OAuth2 |
| ARPAT Toscana | Qualità aria (NO2, O3, PM10, PM2.5) | JSON pubblico |

Per la lista completa con endpoint e stato: `config/sources.yaml`.

---

*Guazza è un progetto personale open source. Pull request benvenute.*

Sviluppato in collaborazione con assistenti AI: **Claude** (Anthropic),
**Gemini** e **DeepSeek** (via OpenRouter) — usati per design review,
debate multi-modello sulle scelte architetturali e supporto all'implementazione.
