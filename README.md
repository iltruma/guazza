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
├── config/                 # Configurazione YAML
│   ├── locations.yaml      # 4 location con coordinate e stazioni SIR associate
│   ├── indicators.yaml     # Soglie indicatori semaforo (DLE)
│   ├── sources.yaml        # Endpoint sorgenti dati e stato
│   └── stations.yaml       # Anagrafica stazioni SIR e ARPAT
├── src/guazza/             # Codice sorgente Python
│   ├── fetchers.py         # Fetcher SIR (storico + realtime) + Netatmo + Open-Meteo
│   ├── storage.py          # DuckDB client + upsert wide
│   ├── weights.py          # Calcolo pesi stazioni per location
│   ├── indicators.py       # Decision Logic Engine (DLE)
│   ├── schema.sql          # Schema DuckDB (unica source of truth)
│   └── jobs/
│       └── ingest.py       # Entry point cron: historical/daily/realtime/forecasts
├── deploy/                 # Template nginx, crontab
├── tests/                  # Test pytest
└── docs/
    ├── status.md           # Stato corrente (aggiornare ogni sessione)
    ├── decisions.md        # Decisioni architetturali motivate
    └── known_issues.md     # Problemi noti + workaround
```

---

## Roadmap

| Sprint | Obiettivo | Stato |
|---|---|---|
| Sprint 0 | Ricognizione sorgenti, config stazioni, struttura repo | Completato |
| Sprint 1 | Ingestion SIR + Netatmo + Open-Meteo, schema DuckDB, job cron | In corso |
| Sprint 2 | LightGBM quantile, CQR, indicatori MVP, frontend base | — |
| Sprint 3 | RH, vento, ARPAT, gelata/nebbia, idrometria, allerte | — |
| Sprint 4 | Benchmark provider, Diebold-Mariano, confronto modelli | — |
| Sprint 5 | Articolo LinkedIn/Medium, cleanup repo, open data | — |

---

## Architettura

- **VPS**: Hetzner CX22 (2 vCPU, 4GB RAM, €3.79/mese)
- **Orchestrazione**: cron Linux (no Prefect, no Airflow)
- **Storage**: DuckDB (colonnare, file singolo) + R2 backup
- **ML**: LightGBM quantile regression + CQR calibration
- **Frontend**: HTML+JS vanilla (Sprint 2)
- **DNS/CDN/WAF**: Cloudflare (gratis)
- **Monitoring**: Healthchecks.io + UptimeRobot (free tier)

Vedi `docs/decisions.md` per motivazioni complete.

---

## Job di ingestion

```bash
# Backfill one-shot (eseguire una volta per caricare lo storico)
uv run python -m guazza.jobs.ingest historical --start-date 2022-01-01

# Delta giornaliero — schedulare a 06:00 UTC
uv run python -m guazza.jobs.ingest daily

# Realtime SIR + Netatmo — schedulare ogni 15-30 min
uv run python -m guazza.jobs.ingest realtime

# Forecast NWP — schedulare ogni 6h (02/08/14/20 UTC)
uv run python -m guazza.jobs.ingest forecasts

# Opzioni comuni
--dry-run          # Simula senza scrivere
--db PATH          # Path file DuckDB (default: /var/lib/guazza/guazza.duckdb)
--config-dir PATH  # Directory YAML config
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

# 5. Installa crontab sul VPS
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
| Open-Meteo Forecast + Historical | Predittori NWP, backfill training | API pubblica, no key |
| SIR Toscana | Ground truth osservazioni validate | Open Data |
| Netatmo | Osservazioni iperlocali real-time | OAuth2 |
| ARPAT Toscana | Qualità aria | JSON pubblico |
| CFR Toscana | Allerte meteo | Scraping HTML |

Per la lista completa con endpoint e stato: `config/sources.yaml`.

---

*Guazza è un progetto personale open source. Pull request benvenute.*
