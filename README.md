# Guazza

> *Guazza* (dal latino *aquatia*): rugiada pesante che si forma nelle conche toscane durante notti serene e umide. Il nome rimanda al fenomeno microclimatico che i modelli standard non catturano.

Previsioni meteo iper-locali per 6 microclimi toscani. Sistema operativo personale + case study tecnico pubblicabile.

**Tesi**: i modelli numerici pubblici (ECMWF, ICON-EU, app commerciali) sbagliano sistematicamente sui microclimi specifici generati da orografia, fondi valle e isole di calore. Questo progetto lo dimostra empiricamente e produce un sistema che fa misurabilmente meglio.

**Infrastruttura**: Dell Optiplex Micro 3050 — homelab NixOS `nebula` (k3s), ~€2/mese (dominio). Pubblico `guazza.it` via Tailscale Funnel (DNS Cloudflare) — da riportare su nebula.

---

## Setup locale

```bash
git clone https://github.com/<tuo-user>/guazza.git
cd guazza
uv sync
cp .env.example .env   # credenziali Netatmo + Uptime Kuma push
```

Prerequisiti: Python 3.13+, [uv](https://docs.astral.sh/uv/).

Verifica:

```bash
uv run ruff check src/ && uv run mypy src/
uv run pytest tests/ -v
DB_PATH=/tmp/guazza_test.duckdb uv run python -m guazza.storage verify-schema
```

---

## Primo forecast

```bash
# 1. Schema DuckDB
uv run python -m guazza.storage init-schema

# 2. Backfill storico (lento — SIR + Open-Meteo 2022→oggi)
uv run python -m guazza.jobs.ingest historical

# 3. Training (o lasciare che review lo faccia al primo run)
uv run python -m guazza.jobs.review run --force-train

# 4. Forecast (feature build, predict, DLE+JSON)
uv run python -m guazza.jobs.forecast run
```

In produzione i job girano come CronJob k8s (namespace `guazza`); per install dev: `deploy/crontab.template`.

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
│   ├── fetch_common.py     # Costanti e helper HTTP condivisi (UA, retry, timezone)
│   ├── fetch_sir.py        # SIR storico CSV + realtime JSON + bulk
│   ├── fetch_openmeteo.py  # Open-Meteo forecast / historical / multi-lead (4 modelli)
│   ├── fetch_netatmo.py    # Netatmo realtime + QC (range, cross, vs SIR)
│   ├── _paths.py           # Path di default da env (DB_PATH, CONFIG_DIR, OUTPUT_DIR)
│   ├── weights.py          # Pesi stazione→location, ring upstream pluvio
│   ├── features.py         # build_features_daily() — 32 feature, tabella materializzata
│   ├── models.py           # LightGBM quantile + CQR, train_all(), predict(), predict_frame()
│   ├── aci.py              # Adaptive Conformal Inference (α_t per target×lead)
│   ├── cv.py               # walk_forward_cv, crps_from_quantiles (case study)
│   ├── hourly_corrector.py # Correttore di forma profilo orario (D-024) + CLI
│   ├── db_queries.py       # Query SQL per current conditions e blend NWP
│   ├── indicators.py       # Decision Logic Engine: evaluate_all(), log_results()
│   ├── output.py           # build_signals(), build_signals_today(), write_location_json(), refresh_realtime_json()
│   ├── qc.py               # Quality control osservazioni SIR (chiamato da ingest)
│   ├── _logging.py         # setup_logging() + log_scrape() — TTY pretty / cron JSON
│   ├── netatmo_daily.py    # Accumulo Netatmo realtime → daily (forward-looking storico)
│   ├── skill_history.py    # append_one(), dump_payload(), atomic_write_json() (usato da review)
│   ├── monitor.py          # compute_coverage(), check_and_log(), update_aci_from_history()
│   └── jobs/
│       ├── _common.py      # Helper job: push Uptime Kuma, job_run(), opzioni typer
│       ├── ingest.py       # historical (backfill one-shot) / realtime
│       ├── forecast.py     # Cron 6h (02/08/14/20 UTC): NWP live → features → predict → JSON
│       └── review.py       # Cron 1×/giorno (06:10 UTC): ingest [ieri-7, ieri] + ACI + skill-history + train condizionale
├── data/                   # guazza.duckdb, models/, output/ (non committati)
├── frontend/               # index.html, affidabilita.html, app.js, style.css (statico, CDN jsDelivr)
├── analysis/               # Strumenti case study (backtest, skill vs gauge primario)
├── Dockerfile              # Single-stage python:3.13-slim + uv + nginx (k8s)
├── deploy/                 # nginx-k8s.conf (k8s, in-container), crontab template
├── tests/
├── DESIGN.md               # Design system frontend (palette Carbone+Iris, tipografia, componenti)
├── PRODUCT.md              # Product brief (utenti, scopo, principi di design)
└── docs/
    ├── status.md           # Stato corrente (cockpit) — leggere a inizio sessione
    ├── decisions.md        # Decisioni architetturali motivate
    ├── contract.md         # Contract JSON di output + logging DLE
    ├── known_issues.md     # Problemi noti + workaround
    └── archive/            # KI risolti (storico)
```

Storia per sprint/release → `CHANGELOG.md`. Coda corrente → `docs/status.md` §Coda.

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

# Realtime SIR + Netatmo — schedulare ogni 15-30 min
uv run python -m guazza.jobs.ingest realtime
```

### Forecast 6h (NWP live → features → predict → JSON)

```bash
# Forecast completo ogni 6h (schedulare a 02:10/08:10/14:10/20:10 UTC)
uv run python -m guazza.jobs.forecast run --db data/guazza.duckdb \
    --model-dir data/models --output-dir data/output

# Dry-run: mostra cosa farebbe senza scrivere
uv run python -m guazza.jobs.forecast run --dry-run
```

Output: `data/output/{location_id}.json` con CI80/CI90 per tmin/tmax/precip,
8 indicatori semaforo (panni, motorino, gelata, ...), `coverage_empirical_30d`,
condizioni realtime aggregate (`current`) e profili orari NWP con confronto modelli.

> **Nota locale**: prima del forecast eseguire `ingest realtime` per avere
> il campo `current` popolato. In produzione il cron ogni 30 min lo mantiene fresco.

### Review giornaliero (obs ieri + ACI + skill-history + train)

```bash
# Review completo 1×/giorno (schedulare a 06:10 UTC)
uv run python -m guazza.jobs.review run --db data/guazza.duckdb \
    --model-dir data/models --output-dir data/output

# Dry-run: solo monitor, nessuna scrittura
uv run python -m guazza.jobs.review run --dry-run
```

## Container image & rilascio

Ad ogni push di un tag `v*.*.*` (es. `v0.9.0`) il workflow CI in
`.github/workflows/ci.yml` builda e pubblica automaticamente l'immagine
container su GitHub Container Registry, allineata a `pyproject.toml`:

- `ghcr.io/iltruma/guazza:v0.9.0` / `ghcr.io/iltruma/guazza:0.9.0`

Immagine single-stage: `python:3.13-slim` + `uv` + nginx (porta 8080, utente
non-root UID 1000), con gli entry point CLI (`guazza-ingest`, `guazza-review`,
`guazza-forecast`, `guazza-storage`, `guazza-hourly-correct`) per i job schedulati.

```bash
# Rilascio
# 1. Bump versione in pyproject.toml (es. 0.9.0 → 0.10.0)
# 2. Sposta [Unreleased] → nuova sezione in CHANGELOG.md
git add pyproject.toml CHANGELOG.md && git commit -m "chore(release): vX.Y.Z"
git tag vX.Y.Z && git push origin main vX.Y.Z

# Uso locale: web (monta la directory dati su /var/lib/guazza)
docker run --rm -p 8080:8080 -v $(pwd)/data:/var/lib/guazza \
    ghcr.io/iltruma/guazza:v0.9.0

# Uso locale: job CLI (es. backfill iniziale)
docker run --rm -v $(pwd)/data:/var/lib/guazza -v $(pwd)/config:/app/config:ro \
    -e KUMA_PUSH_URL=https://kuma.lab.paroparo.it/api/push/<token> \
    -e NETATMO_CLIENT_ID=... -e NETATMO_CLIENT_SECRET=... \
    ghcr.io/iltruma/guazza:v0.9.0 guazza-ingest historical
```

---

## Frontend

SPA statica servita da nginx, senza build step. Layout a 3 sezioni: condizioni
attuali (hero + indicatori DLE su obs realtime), previsioni giornaliere
(card D+0…D+7 con CI bar e tabella NWP), grafico multi-giorno (Chart.js,
switch Guazza ML ↔ 4 modelli NWP), radar RainViewer (Leaflet) e pagina
affidabilità (`affidabilita.html`).

```bash
# Sviluppo locale (il symlink frontend/data → ../data/output deve esistere)
cd frontend && python3 -m http.server 8080
```

Schema canonico dei JSON (`{location_id}.json`, `skill.json`, `skill_history.json`,
DLE `indicator_log`): vedi [`docs/contract.md`](docs/contract.md).

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
