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
# Compila .env con i valori reali
```

### Verifica installazione

```bash
# Lint
uv run ruff check src/ scripts/

# Type check
uv run mypy src/guazza/

# Test
uv run pytest tests/ -v

# Inizializza schema DuckDB di test
DB_PATH=/tmp/guazza_test.duckdb uv run python -m guazza.storage.duckdb_client init-schema
DB_PATH=/tmp/guazza_test.duckdb uv run python -m guazza.storage.duckdb_client verify-schema
```

---

## Task 0 — Ricognizione sorgenti dati

Prima di Sprint 1, eseguire i 4 script di ricognizione. Richiedono connessione internet.

```bash
# Trova stazioni SIR Toscana vicine alle 4 location
uv run python scripts/01_find_sir_stations.py > docs/task0_sir_stations.md

# Trova stazioni ARPAT qualità aria
uv run python scripts/02_find_arpat_stations.py > docs/task0_arpat_stations.md

# Verifica accessibilità endpoint sorgenti dati
uv run python scripts/03_probe_data_sources.py > docs/task0_data_sources.md

# Verifica selettori scraper benchmark provider
uv run python scripts/04_check_benchmark_scrapers.py > docs/task0_scrapers.md
```

Dopo l'esecuzione, compilare `config/stations.yaml` e aggiornare `config/locations.yaml`
con gli ID stazione reali. Rivedere i punti aperti in `docs/status.md`.

---

## Struttura del repo

```
guazza/
├── config/                 # Configurazione YAML
│   ├── locations.yaml      # 4 location utente
│   ├── indicators.yaml     # Soglie indicatori semaforo (DLE)
│   ├── sources.yaml        # Endpoint sorgenti dati
│   └── stations.yaml       # Anagrafica stazioni (DA POPOLARE post-Task 0)
├── src/guazza/             # Codice sorgente Python
│   ├── ingestion/          # Fetcher sorgenti dati (Sprint 1)
│   ├── storage/            # DuckDB client + schema SQL
│   ├── features/           # Feature engineering (Sprint 2)
│   ├── models/             # LightGBM + CQR (Sprint 2)
│   ├── indicators/         # Decision Logic Engine (Sprint 2)
│   ├── evaluation/         # Metriche, calibrazione (Sprint 4)
│   ├── output/             # JSON writer (Sprint 2)
│   └── jobs/               # Entry point cron job
├── scripts/                # Helper Task 0 ricognizione
├── frontend-v1/            # Dashboard HTML+JS vanilla (Sprint 2)
├── notebooks/              # Esplorazione dati e grafici outreach
├── deploy/                 # Template nginx, Caddy, crontab
├── tests/                  # Test pytest
└── docs/
    ├── status.md           # Stato corrente (aggiornare ogni sessione)
    ├── decisions.md        # Decisioni architetturali motivate
    └── known_issues.md     # Problemi noti + workaround
```

---

## Roadmap

| Sprint | Obiettivo | Settimane |
|---|---|---|
| Pre-Sprint (ora) | Bootstrap repo + Task 0 ricognizione | 0 |
| Sprint 1 | Ingestion Open-Meteo + SIR, schema DuckDB, backup R2 | 1-3 |
| Sprint 2 | LightGBM quantile, CQR, indicatori MVP, frontend base | 4-6 |
| Sprint 3 | RH, vento, ARPAT, gelata/nebbia, idrometria, allerte | 7-9 |
| Sprint 4 | Benchmark provider, Diebold-Mariano, confronto modelli | 10-12 |
| Sprint 5 | Articolo LinkedIn/Medium, cleanup repo, open data | 13-16 |

---

## Architettura

- **VPS**: Hetzner CX22 (2 vCPU, 4GB RAM, €3.79/mese)
- **Orchestrazione**: cron Linux (no Prefect, no Airflow)
- **Storage**: DuckDB (colonnare, file singolo) + Parquet raw + R2 backup
- **ML**: LightGBM quantile regression + CQR calibration
- **Frontend**: HTML+JS vanilla (Sprint 1-2), eventualmente React+Vite (Sprint 6+)
- **DNS/CDN/WAF**: Cloudflare (gratis)
- **Monitoring**: Healthchecks.io + UptimeRobot (free tier)

Scelte validate da debate strutturato multi-modello (Claude + Gemini + DeepSeek).
Vedi `docs/decisions.md` per motivazioni complete.

---

## Come ricostruire da zero

```bash
# 1. Clone e setup
git clone https://github.com/<user>/guazza.git && cd guazza
uv sync && cp .env.example .env

# 2. Edita .env con credenziali reali

# 3. Inizializza schema DuckDB
uv run python -m guazza.storage.duckdb_client init-schema

# 4. (Primo avvio) Esegui Task 0 per ricognizione sorgenti
uv run python scripts/01_find_sir_stations.py > docs/task0_sir_stations.md
# ... (vedi sezione Task 0 sopra)

# 5. (Sprint 1) Backfill dati storici
# uv run guazza-ingest-forecasts --location casa_campi --backfill

# 6. Installa crontab sul VPS
# crontab deploy/crontab.template
```

---

## Sviluppo

```bash
# Test con coverage
uv run pytest tests/ --cov=guazza --cov-report=term-missing

# Lint + format check
uv run ruff check src/ scripts/ tests/
uv run ruff format --check src/ scripts/ tests/

# Type check
uv run mypy src/guazza/
```

Le PR vengono validate automaticamente da `.github/workflows/ci.yml`.

---

## Sorgenti dati

| Sorgente | Uso | Accesso |
|---|---|---|
| Open-Meteo Forecast + Historical | Predittori NWP, backfill training | API pubblica, no key |
| SIR Toscana | Ground truth osservazioni validate | Open Data (CKAN) |
| CFR Toscana | Real-time + idrometria Bisenzio | Scraping HTML |
| ARPAT Toscana | Qualità aria | Da verificare (JSON o HTML) |
| Yr.no | Benchmark | API ufficiale MET Norway |
| 3BMeteo, iLMeteo | Benchmark | Scraping HTML aggregato |

Per la lista completa con endpoint e stato: `config/sources.yaml`.

---

*Guazza è un progetto personale open source. Pull request benvenute.*
