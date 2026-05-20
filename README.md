# Guazza

> *Guazza* (dal latino *aquatia*): rugiada pesante che si forma nelle conche toscane durante notti serene e umide. Il nome rimanda al fenomeno microclimatico che i modelli standard non catturano.

Previsioni meteo iper-locali per 5 microclimi toscani. Sistema operativo personale + case study tecnico pubblicabile.

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
│   ├── locations.yaml      # 5 location con stazioni SIR e upstream_pluvio_stations
│   ├── stations.yaml       # 34 stazioni SIR (21 operative + 13 upstream pluvio ring); ARPAT non più usate (cutover OpenAQ v0.6.2)
│   ├── indicators.yaml     # 8 indicatori DLE con soglie e costi asimmetrici
│   ├── arpat_levels.yaml   # Scale qualità aria D.Lgs.155/2010
│   └── sources.yaml        # Endpoint sorgenti dati e stato
├── src/guazza/
│   ├── schema.sql          # Schema DuckDB — unica source of truth
│   ├── storage.py          # DuckDBClient, upsert bulk Arrow, backfill_prediction_obs
│   ├── fetchers.py         # SIR storico/realtime, Netatmo, Open-Meteo (6 modelli), OpenAQ v3
│   ├── weights.py          # Pesi stazione→location, ring upstream pluvio
│   ├── features.py         # build_features_daily() — 50 feature, tabella materializzata
│   ├── models.py           # LightGBM quantile + CQR, train_all(), predict()
│   ├── indicators.py       # Decision Logic Engine: evaluate_all(), log_results()
│   ├── output.py           # build_signals(), build_signals_today(), dewpoint/apparent_temp, write_location_json()
│   ├── qc.py               # Quality control osservazioni SIR + OpenAQ
│   ├── _logging.py         # setup_logging() — TTY pretty / cron JSON strutturato
│   └── jobs/
│       ├── ingest.py       # Cron: historical / daily / realtime / forecasts
│       ├── features.py     # CLI: features build / info
│       ├── train.py        # One-shot: train run / train eval (walk-forward CV)
│       ├── predict.py      # Cron: predict → DuckDB + DLE + JSON output
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
| Sprint 6 | Frontend HTML+JS+Tailwind+DaisyUI+Chart.js, layout Foreca a 3 sezioni | ✅ Completato |
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
- **Frontend**: HTML+JS vanilla + Tailwind CSS + DaisyUI v4 + Chart.js (CDN, statico)
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
# Refresh condizioni realtime (necessario per il campo `current` nel JSON)
uv run python -m guazza.jobs.ingest realtime

# Genera predizioni quantile + DLE + JSON (schedulare ogni 6h dopo il job forecasts)
uv run python -m guazza.jobs.predict --db data/guazza.duckdb \
    --model-dir data/models --output-dir data/output
```

Output: `data/output/{location_id}.json` con CI80/CI90 per tmin/tmax/precip,
8 indicatori semaforo (panni, motorino, gelata, ...), `coverage_empirical_30d`,
condizioni realtime aggregate (`current` con dewpoint e temperatura percepita),
qualità aria OpenAQ (`air_quality`), profili orari NWP con vento, e confronto
modelli con data ultimo run.

> **Nota locale**: prima di `predict` eseguire `ingest realtime` per avere
> il campo `current` popolato. In produzione il cron ogni 30 min lo mantiene fresco.

### Opzioni comuni

```bash
--dry-run    # Simula senza scrivere
--db PATH    # Path DuckDB (default prod: /var/lib/guazza/guazza.duckdb)
```

Variabile d'ambiente `HEALTHCHECKS_URL` per il ping dead-man switch.

---

## Frontend

Il frontend è una SPA statica servita da nginx. Non richiede build step.

### Sviluppo locale

```bash
# Il symlink frontend/data → ../data/output deve esistere (già nel repo)
cd frontend && python3 -m http.server 8080
# Apri http://localhost:8080
```

### Layout (3 sezioni stile Foreca)

| Sezione | Contenuto |
|---|---|
| **A — Condizioni attuali** | Temperatura grande, icona meteo, temperatura percepita (Steadman), punto di rugiada (Magnus), vento/umidità/precipitazione realtime SIR, indicatori DLE calcolati su obs realtime, card qualità aria OpenAQ (PM10, PM2.5, NO₂, O₃, CO, benzene, SO₂ — sempre tutti i 7 indicatori, `—` se non misurati) |
| **B — Previsioni giornaliere** | Striscia card D+0…D+7 con icona/Tmax/Tmin/precip/indicator-dots; clic espande CI bar 80/90% + 8 indicatori + tabella NWP con data ultimo run |
| **C — Grafico multi-giorno** | Chart.js: temperatura, umidità, precipitazioni, vento — switch Guazza ML ↔ 6 modelli NWP, crosshair verticale |

### Struttura file frontend

```
frontend/
├── index.html      # HTML + CDN links (Tailwind, DaisyUI v4, Chart.js)
├── app.js          # Logica completa (rendering, chart, routing)
├── style.css       # Solo CI bar + indicatori DLE + chart scroll (~25 righe)
└── data/           # Symlink → ../data/output (JSON per ogni location)
```

### JSON di output (`data/output/{location_id}.json`)

```
{location_id, generated_at, coverage_empirical_30d,
 current: {ts, temp_c, humidity_pct, precip_mm, wind_speed_ms, dewpoint_c, feels_like_c},
 air_quality: {pm10_ugm3, pm25_ugm3, no2_ugm3, o3_ugm3, co_mgm3, benzene_ugm3, so2_ugm3},
 nwp_models_hourly: [{source, label, data: [{ts, temp_c, humidity_pct, precip_mm, wind_speed_ms}]}],
 days: [{target_date, lead_time_h,
         forecasts: {tmin_c, tmax_c, precip_mm} ciascuno con p50+CI80+CI90,
         indicators: {panni, motorino, gelata, ...} con verdict+rule_matched,
         hourly: [{hour, temp_c, humidity_pct, precip_mm, precip_prob, wind_speed_ms}],
         nwp_comparison: [{source, label, tmin_c, tmax_c, precip_mm, last_run}]}]}
```

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
uv run python -m guazza.jobs.predict

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
| OpenAQ v3 | Qualità aria multi-provider (include ARPAT Toscana upstream) | `X-API-Key` header (`OPENAQ_API_KEY`) |

Per la lista completa con endpoint e stato: `config/sources.yaml`.

---

*Guazza è un progetto personale open source. Pull request benvenute.*

Sviluppato in collaborazione con assistenti AI: **Claude** (Anthropic),
**Gemini** e **DeepSeek** (via OpenRouter) — usati per design review,
debate multi-modello sulle scelte architetturali e supporto all'implementazione.
