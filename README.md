# Guazza

> *Guazza* (dal latino *aquatia*): rugiada pesante che si forma nelle conche toscane durante notti serene e umide. Il nome rimanda al fenomeno microclimatico che i modelli standard non catturano.

Previsioni meteo iper-locali per 6 microclimi toscani. Sistema operativo personale + case study tecnico pubblicabile.

**Tesi**: i modelli numerici pubblici (ECMWF, ICON-EU, app commerciali) sbagliano sistematicamente sui microclimi specifici generati da orografia, fondi valle e isole di calore. Questo progetto lo dimostra empiricamente e produce un sistema che fa misurabilmente meglio.

**Costo infrastruttura**: ~€2/mese (dominio). Server: Dell Optiplex Micro 3050 — homelab NixOS `nebula` (k3s). Accesso interno `guazza.lab.paroparo.it`; pubblico `guazza.it` via Tailscale Funnel (DNS Cloudflare) — da riportare su nebula.

---

## Setup locale

### Prerequisiti

- Python 3.13+
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
│   ├── stations.yaml       # 34 stazioni SIR (21 operative + 13 upstream pluvio ring)
│   ├── indicators.yaml     # 8 indicatori DLE con soglie e costi asimmetrici
│   └── sources.yaml        # Endpoint sorgenti dati e stato
├── src/guazza/
│   ├── schema.sql          # Schema DuckDB — unica source of truth (+ vista obs_weighted_daily)
│   ├── storage.py          # DuckDBClient, upsert bulk Arrow, backfill_prediction_obs
│   ├── fetchers.py         # CLI fetcher (sir-historical / sir-realtime / netatmo)
│   ├── fetch_common.py     # Costanti e helper HTTP condivisi (UA, retry, timezone)
│   ├── fetch_sir.py        # SIR storico CSV + realtime JSON + bulk
│   ├── fetch_openmeteo.py  # Open-Meteo forecast / historical / multi-lead (4 modelli)
│   ├── fetch_netatmo.py    # Netatmo realtime + QC (range, cross, vs SIR)
│   ├── _paths.py           # Path di default da env (DB_PATH, CONFIG_DIR, OUTPUT_DIR)
│   ├── weights.py          # Pesi stazione→location, ring upstream pluvio
│   ├── features.py         # build_features_daily() — 50 feature, tabella materializzata
│   ├── models.py           # LightGBM quantile + CQR, train_all(), predict(), predict_frame()
│   ├── indicators.py       # Decision Logic Engine: evaluate_all(), log_results() (eval via AST)
│   ├── output.py           # build_signals(), build_signals_today(), dewpoint/apparent_temp, write_location_json()
│   ├── qc.py               # Quality control osservazioni SIR (chiamato da ingest)
│   ├── _logging.py         # setup_logging() + log_scrape() — TTY pretty / cron JSON
│   ├── netatmo_daily.py    # Accumulo Netatmo realtime → daily (forward-looking storico)
 │   ├── skill_history.py    # append_one(), dump_payload(), atomic_write_json() (usato da pipeline)
 │   ├── monitor.py          # compute_coverage(), check_and_log() (usato da pipeline)
 │   └── jobs/
 │       ├── _common.py      # Helper job: ping Healthchecks, job_run(), opzioni typer
 │       ├── ingest.py       # Cron: historical / daily / realtime
 │       ├── pipeline.py     # Cron 6h: forecasts → features → predict → skill-history → monitor
 │       ├── train.py        # One-shot: train run / train eval (walk-forward CV)
 │       ├── skill.py        # Cron settimanale: curva skill MAE per lead → skill.json
 │       └── backup.py       # Cron: backup DuckDB su Cloudflare R2
├── data/
│   ├── guazza.duckdb       # Database analitico (non committato)
│   ├── models/             # Artefatti LightGBM artifacts.json + model-string .txt (non committati)
│   └── output/             # JSON per il frontend (non committati)
├── frontend/               # index.html, app.js, style.css (statico, CSS custom, CDN via jsDelivr)
├── Dockerfile              # Single-stage python:3.13-slim + uv + nginx (k8s)
├── .dockerignore           # Esclude .venv/, data/, tests/, ...
├── .github/workflows/ci.yml # CI su push tag v*.*.* → ghcr.io/iltruma/guazza
├── deploy/                 # nginx-k8s.conf (k8s, in-container), crontab template
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

Sprint completati (0–13). Storia dettagliata per release → `CHANGELOG.md`.
Coda corrente (P-items e decisioni approvate da implementare) → `docs/status.md` §Coda.

| Sprint | Obiettivo | Stato |
|---|---|---|
| Sprint 0 | Ricognizione sorgenti, config stazioni, struttura repo | ✅ Completato |
| Sprint 1 | Ingestion SIR + Netatmo + Open-Meteo + ARPAT (rimosso v0.13.0), schema DuckDB, job cron | ✅ Completato |
| Sprint 2 | Backfill SIR pre-2022, quality control (SIR), flag qualità | ✅ Completato |
| Sprint 3 | Feature engineering, 50 feature, ring upstream pluvio | ✅ Completato |
| Sprint 4 | LightGBM quantile + CQR, skill +25% vs NWP su temperatura | ✅ Completato |
| Sprint 5 | Output JSON, Decision Logic Engine, indicatori operativi | ✅ Completato |
| Sprint 6 | Frontend HTML+JS+Chart.js, layout a 3 sezioni | ✅ Completato |
| Sprint 7 | Raffinamenti logiche, radar RainViewer, redesign frontend v2 (CSS custom) | ✅ Completato |
| Sprint 8 | Pipeline unificata + ICON-D2 rimosso + realtime refresh (v0.12.0–v0.12.6) | ✅ Completato |
| Sprint 9 | Adaptive Conformal Inference + monitor copertura 30d (v0.10.0) | ✅ Completato |
| Sprint 10 | Skill history time series + GFS rimosso + CQR fix (v0.11.0–v0.11.2) | ✅ Completato |
| Sprint 11 | Calibrazione soglie DLE post-30gg `indicator_log` in produzione | 🟡 In corso |
| Sprint 12 | Case study / pubblicazione (articolo LinkedIn/Medium, repo pubblico) | 🔴 Da fare |
| Sprint 13 | Semplificazione documentale + archivio KI risolti + status cockpit | ✅ Completato (commit recenti) |
| Sprint 8 | Deploy homelab: k3s + Flux + SOPS, PVC, CronJob, immagine container | ✅ Completato |
| Sprint 9 | Adaptive Conformal Inference + monitor copertura 30d | ✅ Completato |

---

## Architettura

- **Server**: Dell Optiplex Micro 3050 — host NixOS `nebula` (k3s), Guazza è un tenant
- **Container**: `ghcr.io/iltruma/guazza` (Python 3.13 + uv + nginx), buildato su tag `v*.*.*`
- **Scheduling**: k8s CronJob (namespace `guazza`); cron Linux per dev
- **Storage**: DuckDB (file singolo) + backup R2
- **ML**: LightGBM quantile + CQR + ACI
- **Frontend**: HTML+JS vanilla + CSS custom + Chart.js + Leaflet
- **Esposizione**: Tailscale Funnel (pubblico) + Tailscale/Traefik (tailnet)

Tabella completa di stack, anti-pattern e invarianti in `AGENTS.md` §"Stack blindato".
Motivazioni delle scelte in `docs/decisions.md`.

---

## Pipeline operativa

### Ingestion

```bash
# Backfill storico one-shot (SIR + Open-Meteo historical lead=0 + multilead lead 24-168h, 2022→oggi)
uv run python -m guazza.jobs.ingest historical

# Delta giornaliero — schedulare a 06:00 UTC (SIR + OM historical + OM multilead + Netatmo daily)
uv run python -m guazza.jobs.ingest daily

# Backfill Netatmo daily su tutti i giorni accumulati (one-shot, prima esecuzione):
uv run python -m guazza.jobs.ingest daily --netatmo-all

# Realtime SIR + Netatmo — schedulare ogni 15-30 min
uv run python -m guazza.jobs.ingest realtime
```

### Pipeline 6h (forecasts → features → predict → skill-history)

```bash
# Pipeline completa ogni 6h (schedulare a 02/08/14/20 UTC)
uv run python -m guazza.jobs.pipeline run --db data/guazza.duckdb \
    --model-dir data/models --output-dir data/output

# Dry-run: mostra cosa farebbe senza scrivere
uv run python -m guazza.jobs.pipeline run --dry-run
```

Output: `data/output/{location_id}.json` con CI80/CI90 per tmin/tmax/precip,
8 indicatori semaforo (panni, motorino, gelata, ...), `coverage_empirical_30d`,
condizioni realtime aggregate (`current` con dewpoint e temperatura percepita),
profili orari NWP con vento, e confronto
modelli con data ultimo run.

> **Nota locale**: prima della pipeline eseguire `ingest realtime` per avere
> il campo `current` popolato. In produzione il cron ogni 30 min lo mantiene fresco.

### Training (on-demand o mensile)

```bash
# Pesi stazioni + ring upstream pluvio
uv run python -m guazza.weights refresh

# Training LightGBM + CQR
uv run python -m guazza.jobs.train run --db data/guazza.duckdb --model-dir data/models

# Walk-forward CV con metriche
uv run python -m guazza.jobs.train eval --db data/guazza.duckdb
```

### Skill history backfill manuale

```bash
# append + dump sono inclusi automaticamente nella pipeline 6h.
# Per backfill manuale (es. dopo perdita dati):
python -c "
import duckdb
from guazza.skill_history import append_one, dump_payload, atomic_write_json, DEFAULT_DUMP_PATH
from datetime import date, timedelta
con = duckdb.connect('data/guazza.duckdb')
for i in range(30):
    append_one(con, date.today() - timedelta(days=i+1))
atomic_write_json(DEFAULT_DUMP_PATH, dump_payload(con))
"
```

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
| **A — Condizioni attuali** | Temperatura grande, icona meteo, temperatura percepita (Steadman), punto di rugiada (Magnus), grid stats: vento (velocità + direzione), umidità, precipitazione, pressione (hPa + indicatore alta/bassa), alba/tramonto (SunCalc), fase lunare; indicatori DLE calcolati su obs realtime |
| **Radar precipitazioni** | Mappa Leaflet con overlay RainViewer: ultimi ~60min osservati + nowcast +60min (se attivo); timeline animata, pausa di default, zoom custom |
| **B — Previsioni giornaliere** | Striscia card D+0…D+7 con icona/Tmax/Tmin/precip/indicator-dots; clic espande CI bar 80/90% + 8 indicatori + tabella NWP con data ultimo run |
| **C — Grafico multi-giorno** | Chart.js: temperatura, umidità, precipitazioni, vento — switch Guazza ML ↔ 4 modelli NWP, crosshair verticale |

### Struttura file frontend

```
frontend/
├── index.html      # HTML + CDN links (Chart.js, Leaflet, Twemoji, SunCalc; font Geist + JetBrains Mono)
├── app.js          # Logica completa (rendering, chart, radar, routing)
├── style.css       # CSS custom (classi g-*): palette Carbone+Iris, CI bar, indicatori DLE, Leaflet overrides, Twemoji fix
└── data/           # Symlink → ../data/output (JSON per ogni location)
```

### JSON di output (`data/output/{location_id}.json`)

Schema canonico (output per location, `skill.json` globale, `skill_history.json`,
DLE `indicator_log`): vedi [`docs/contract.md`](docs/contract.md).

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

# 6. Training modello
uv run python -m guazza.jobs.train run

# 7. Prima pipeline (include feature build, predict, DLE+JSON, monitor)
uv run python -m guazza.jobs.pipeline run

# 8. In produzione i job girano come CronJob k8s (namespace `guazza`, repo astra).
#    Solo per install dev: crontab deploy/crontab.template
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
| Open-Meteo Forecast + Historical | 4 modelli NWP (ECMWF, ICON-EU, AROME, ICON-2I) | API pubblica, no key |
| SIR Toscana | Ground truth osservazioni validate, 34 stazioni | Open Data |
| Netatmo | Osservazioni iperlocali real-time | OAuth2 |
| RainViewer | Radar precipitazioni (solo frontend) | API pubblica, no key |

Per la lista completa con endpoint e stato: `config/sources.yaml`.

---

*Guazza è un progetto personale open source. Pull request benvenute.*

Sviluppato in collaborazione con assistenti AI: **Claude** (Anthropic),
**Gemini** e **DeepSeek** (via OpenRouter) — usati per design review,
debate multi-modello sulle scelte architetturali e supporto all'implementazione.
