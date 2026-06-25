# Guazza

> *Guazza* (dal latino *aquatia*): rugiada pesante che si forma nelle conche toscane durante notti serene e umide. Il nome rimanda al fenomeno microclimatico che i modelli standard non catturano.

Previsioni meteo iper-locali per 6 microclimi toscani. Sistema operativo personale + case study tecnico pubblicabile.

**Tesi**: i modelli numerici pubblici (ECMWF, ICON-EU, app commerciali) sbagliano sistematicamente sui microclimi specifici generati da orografia, fondi valle e isole di calore. Questo progetto lo dimostra empiricamente e produce un sistema che fa misurabilmente meglio.

**Costo infrastruttura**: ~€2/mese (dominio). Server: Dell Optiplex Micro 3050 locale, esposto via Cloudflare Tunnel.

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
│   ├── locations.yaml      # 6 location con stazioni SIR e upstream_pluvio_stations
│   ├── stations.yaml       # 34 stazioni SIR (21 operative + 13 upstream pluvio ring); stazioni ARPAT qualità aria in locations.yaml (arpat_stations)
│   ├── indicators.yaml     # 8 indicatori DLE con soglie e costi asimmetrici
│   ├── arpat_levels.yaml   # Scale qualità aria D.Lgs.155/2010
│   └── sources.yaml        # Endpoint sorgenti dati e stato
├── src/guazza/
│   ├── schema.sql          # Schema DuckDB — unica source of truth (+ vista obs_weighted_daily)
│   ├── storage.py          # DuckDBClient, upsert bulk Arrow, backfill_prediction_obs
│   ├── fetchers.py         # CLI fetcher (sir-historical / sir-realtime / netatmo)
│   ├── fetch_common.py     # Costanti e helper HTTP condivisi (UA, retry, timezone)
│   ├── fetch_sir.py        # SIR storico CSV + realtime JSON + bulk
│   ├── fetch_openmeteo.py  # Open-Meteo forecast / historical / multi-lead (6 modelli)
│   ├── fetch_netatmo.py    # Netatmo realtime + QC (range, cross, vs SIR)
│   ├── fetch_arpat.py      # ARPAT qualità aria NRT + bollettino PM10/PM2.5
│   ├── _paths.py           # Path di default da env (DB_PATH, CONFIG_DIR, OUTPUT_DIR)
│   ├── weights.py          # Pesi stazione→location, ring upstream pluvio
│   ├── features.py         # build_features_daily() — 50 feature, tabella materializzata
│   ├── models.py           # LightGBM quantile + CQR, train_all(), predict(), predict_frame()
│   ├── indicators.py       # Decision Logic Engine: evaluate_all(), log_results() (eval via AST)
│   ├── output.py           # build_signals(), build_signals_today(), dewpoint/apparent_temp, write_location_json()
│   ├── qc.py               # Quality control osservazioni SIR + ARPAT
│   ├── _logging.py         # setup_logging() + log_scrape() — TTY pretty / cron JSON
│   └── jobs/
│       ├── _common.py      # Helper job: ping Healthchecks, job_run(), opzioni typer
│       ├── ingest.py       # Cron: historical / daily / realtime / forecasts / multilead
│       ├── features.py     # CLI: features build / info
│       ├── train.py        # One-shot: train run / train eval (walk-forward CV)
│       ├── predict.py      # Cron: predict → DuckDB + DLE + JSON output
│       ├── skill.py        # Cron: curva skill MAE per lead → skill.json
│       ├── netatmo_daily.py # Cron: aggregazione Netatmo realtime → daily
│       ├── qc.py           # Cron: qc run / qc report
│       └── backup.py       # Cron: backup DuckDB su Cloudflare R2 (Sprint 8)
├── data/
│   ├── guazza.duckdb       # Database analitico (non committato)
│   ├── models/             # Artefatti LightGBM pickle (non committati)
│   └── output/             # JSON per il frontend (non committati)
├── frontend/               # index.html, app.js, style.css (statico, CSS custom, CDN via jsDelivr)
├── Dockerfile              # Single-stage python:3.13-slim + uv + nginx (k8s)
├── .dockerignore           # Esclude .venv/, data/, tests/, ...
├── .github/workflows/ci.yml # CI su push tag v*.*.* → ghcr.io/iltruma/guazza
├── deploy/                 # nginx.conf (host-path), nginx-k8s.conf (k8s), Caddyfile, crontab template
├── tests/
├── DESIGN.md               # Design system frontend (palette Carbone+Iris, tipografia, componenti)
├── PRODUCT.md              # Product brief (utenti, scopo, principi di design)
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
| Sprint 6 | Frontend HTML+JS+Chart.js, layout a 3 sezioni | ✅ Completato |
| Sprint 7 | Raffinamenti logiche, radar RainViewer, redesign frontend v2 (CSS custom) | 🟡 In corso |
| Sprint 8 | Deploy su Optiplex locale + Cloudflare Tunnel, k3s/ArgoCD, immagine container | 🟡 In corso (S-A: Dockerfile + CI) |
| Sprint 9 | Model monitoring, coverage alert | — |
| Sprint 10 | Calibrazione soglie DLE post-deploy | — |
| Sprint 11 | Case study / pubblicazione | — |

---

## Architettura

- **Server**: Dell Optiplex Micro 3050 — host Proxmox (homelab multi-servizio, Guazza è un tenant)
- **Container**: immagine `ghcr.io/iltruma/guazza` (Python 3.13 + nginx, single-stage con `uv`), buildata su push tag `v*.*.*`
- **Scheduling**: cron Linux o k8s CronJob — job = CLI idempotenti orchestrator-agnostic
- **Storage**: DuckDB (colonnare, file singolo) + R2 backup
- **ML**: LightGBM quantile regression + CQR calibration
- **Frontend**: HTML+JS vanilla + CSS custom (no framework) + Chart.js + Leaflet + Twemoji + suncalc; font Geist + JetBrains Mono (CDN, statico)
- **Esposizione**: Cloudflare Tunnel (`cloudflared`) — nessun IP pubblico, SSL automatico
- **DNS/CDN/WAF**: Cloudflare (gratis)
- **Monitoring**: Healthchecks.io + UptimeRobot (free tier)

Vedi `docs/decisions.md` per motivazioni complete.

---

## Pipeline operativa

### Ingestion

```bash
# Backfill storico one-shot (SIR + Open-Meteo 2022→oggi)
uv run python -m guazza.jobs.ingest historical

# Backfill multi-lead D+1…D+7 one-shot (run precedenti via *_previous_dayN, per il backtest)
uv run python -m guazza.jobs.ingest multilead

# Delta giornaliero — schedulare a 06:00 UTC (include l'accumulo Netatmo daily di ieri)
uv run python -m guazza.jobs.ingest daily

# Realtime SIR + Netatmo — schedulare ogni 15-30 min
uv run python -m guazza.jobs.ingest realtime

# Accumulo Netatmo realtime → daily (storico forward-looking, non-training).
# Già incluso in `ingest daily`; standalone per backfill dell'accumulato:
uv run python -m guazza.jobs.netatmo_daily run --all

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
qualità aria ARPAT OpenData NRT (`air_quality`), profili orari NWP con vento, e confronto
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

## Container image & rilascio

Ad ogni push di un tag `v*.*.*` (es. `v0.9.0`) il workflow CI in
`.github/workflows/ci.yml` builda e pubblica automaticamente l'immagine
container su GitHub Container Registry, allineata a `pyproject.toml`:

- `ghcr.io/iltruma/guazza:v0.9.0`
- `ghcr.io/iltruma/guazza:0.9.0`

L'immagine è single-stage: `python:3.13-slim` + `uv` (binario ufficiale) + nginx + frontend statico. Il
container gira come utente non-root (UID 1000), include nginx sulla porta 8080
per il servizio web e gli entry point CLI (`guazza-ingest`, `guazza-predict`,
`guazza-train`, ...) per i job schedulati.

### Procedura di rilascio

```bash
# 1. Bump versione in pyproject.toml (es. 0.9.0 → 0.10.0)
# 2. Aggiorna CHANGELOG.md (sposta [Unreleased] → nuova sezione versionata)
# 3. Commit
git add pyproject.toml CHANGELOG.md
git commit -m "chore(release): vX.Y.Z"

# 4. Crea e pusha il tag — triggera il workflow CI
git tag vX.Y.Z
git push origin main vX.Y.Z
```

### Uso locale dell'immagine

```bash
docker pull ghcr.io/iltruma/guazza:v0.9.0

# Web (monta la directory dati locale su /var/lib/guazza)
docker run --rm -p 8080:8080 \
    -v $(pwd)/data:/var/lib/guazza \
    ghcr.io/iltruma/guazza:v0.9.0

# Job CLI (es. backfill iniziale)
docker run --rm \
    -v $(pwd)/data:/var/lib/guazza \
    -v $(pwd)/config:/app/config:ro \
    -e HEALTHCHECKS_URL=https://hc-ping.com/<uuid> \
    -e NETATMO_CLIENT_ID=... -e NETATMO_CLIENT_SECRET=... \
    ghcr.io/iltruma/guazza:v0.9.0 \
    guazza-ingest historical
```

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
| **A — Condizioni attuali** | Temperatura grande, icona meteo, temperatura percepita (Steadman), punto di rugiada (Magnus), grid stats: vento (velocità + direzione), umidità, precipitazione, pressione (hPa + indicatore alta/bassa), alba/tramonto (SunCalc), fase lunare; card qualità aria ARPAT (PM10, PM2.5, NO₂, O₃, CO, benzene, SO₂); indicatori DLE calcolati su obs realtime |
| **Radar precipitazioni** | Mappa Leaflet con overlay RainViewer: ultimi ~60min osservati + nowcast +60min (se attivo); timeline animata, pausa di default, zoom custom |
| **B — Previsioni giornaliere** | Striscia card D+0…D+7 con icona/Tmax/Tmin/precip/indicator-dots; clic espande CI bar 80/90% + 8 indicatori + tabella NWP con data ultimo run |
| **C — Grafico multi-giorno** | Chart.js: temperatura, umidità, precipitazioni, vento — switch Guazza ML ↔ 6 modelli NWP, crosshair verticale |

### Struttura file frontend

```
frontend/
├── index.html      # HTML + CDN links (Chart.js, Leaflet, Twemoji, SunCalc; font Geist + JetBrains Mono)
├── app.js          # Logica completa (rendering, chart, radar, routing)
├── style.css       # CSS custom (classi g-*): palette Carbone+Iris, CI bar, indicatori DLE, Leaflet overrides, Twemoji fix
└── data/           # Symlink → ../data/output (JSON per ogni location)
```

### JSON di output (`data/output/{location_id}.json`)

```
{location_id, generated_at, coverage_empirical_30d,
 current: {ts, temp_c, humidity_pct, precip_mm, wind_speed_ms, wind_dir_deg, dewpoint_c, feels_like_c, pressure_hpa},
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

# 9. Installa crontab sul server locale
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
| ARPAT OpenData NRT | Qualità aria oraria (NO₂, O₃, CO, SO₂, PM10, PM2.5, benzene) | Open Data |
| RainViewer | Radar precipitazioni (solo frontend) | API pubblica, no key |

Per la lista completa con endpoint e stato: `config/sources.yaml`.

---

*Guazza è un progetto personale open source. Pull request benvenute.*

Sviluppato in collaborazione con assistenti AI: **Claude** (Anthropic),
**Gemini** e **DeepSeek** (via OpenRouter) — usati per design review,
debate multi-modello sulle scelte architetturali e supporto all'implementazione.
