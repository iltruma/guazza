# Guazza — Briefing per Claude Code

> Leggi questo file all'inizio di ogni sessione. Poi leggi `docs/status.md` per sapere dove siamo.

## Cos'è questo progetto

**Guazza** è un sistema di previsioni meteo iper-locali per microclimi toscani specifici. Duplice scopo:

1. **Strumento personale**: previsioni affinate per 4 location dell'utente con indicatori operativi diretti (panni, motorino, gelata, ecc.)
2. **Case study tecnico pubblicabile**: articolo unico LinkedIn/Medium con metodologia rigorosa, bibliografia scientifica, repo pubblico

**Tesi**: le previsioni pubbliche (ECMWF, LAMMA, 3BMeteo, ecc.) sbagliano sistematicamente sui microclimi specifici. Vogliamo dimostrarlo con dati e fare meglio, ammettendo onestamente dove falliamo.

## Dove siamo ora

Leggi **`docs/status.md`** — è l'unica fonte di verità sullo stato corrente.

## Utente

Cloud Architect con background ML applicato. Sviluppa nel tempo libero, a sprint irregolari. Conosce bene Python, Klipper, CFD, infrastructure cloud. Non ha bisogno di spiegazioni elementari. Preferisce risposte dirette e concrete.

**Non richiede**: spiegazioni ovvie, preamble, riepiloghi finali, domande multiple. Una sola domanda per turno se serve chiarimento.

## Stack tecnico — decisioni blindate

Queste scelte sono state validate da debate multi-modello (Claude + Gemini + DeepSeek). **Non proporre alternative** a meno che non ci sia un bug tecnico reale che le impone.

| Componente | Scelta | Motivazione |
|---|---|---|
| VPS | Hetzner CX22 (€3.79/mese) | Single node, budget |
| OS | Ubuntu 24.04 LTS | LTS, standard |
| Orchestrazione | cron Linux | Stupido, robusto, prevedibile |
| Storage analitico | DuckDB | Column-oriented, file singolo, backup = cp |
| Storage raw NWP | Parquet partizionato | Compresso, leggibile con pandas/polars |
| Backup | Cloudflare R2 (10GB free) | Egress gratis, free tier |
| ML core | LightGBM quantile | Gold standard dati tabulari, no GPU |
| CI calibrazione | CQR (Romano 2019) | Garanzia copertura marginale |
| Deploy | GitHub Actions → SSH al VPS | Solo CI/CD, non orchestration |
| Frontend V1 | HTML + JS vanilla + Nginx | Zero dipendenze, statico |
| DNS/CDN/WAF | Cloudflare | Gratis |
| Monitoring | Healthchecks.io + UptimeRobot | Free tier, dead-man switch |
| Retry scraper | tenacity | Exponential backoff, standard |
| Logging | loguru | JSON strutturato |
| Validation | pydantic v2 | Type safety |
| HTTP | httpx (sync) | Niente async overhead per cron |

**Non proporre mai**: Coolify, Prefect, Dagster, Airflow, Kubernetes, ArgoCD, GitHub Actions come orchestratore runtime, PostgreSQL, Redis, Celery.

## Decisioni scientifiche — blindate

Vedi **`docs/decisions.md`** per il dettaglio completo. Sintesi:

- **ERA5 mai come predittore di forecast** — solo come climatologia statica o ground truth alternativo. Usarlo come predittore = train/serve skew grave.
- **Embargo 7 giorni in CV** — autocorrelazione sinottica. Meno = metriche gonfiate.
- **CQR stratificato per lead time bucket** — un calibration set separato per: 0-6h, 6-12h, 12-24h, 24-48h, 48-72h
- **`coverage_empirical_30d`** nel JSON di output — onestà verso l'utente su quanto fidarsi del CI
- **Modello globale con location-id categorica** — no modelli per-location indipendenti
- **Ogni previsione è una distribuzione** — mai valori puntuali nudi senza CI
- **Indicatori operativi sono il prodotto** — non feature secondarie

## Le 4 location

```yaml
casa_campi:      # Campi Bisenzio (FI), ~35m
lavoro_cosimo:   # Scandicci (FI), ~50m
lavoro_madda:    # Prato, ~60m
casa_cesto:      # Figline Valdarno (FI), ~200m
```

Vedi `config/locations.yaml` per coordinate complete.

## Sorgenti dati principali

- **Open-Meteo Forecast + Historical Forecast API** — multi-modello (ECMWF, ICON-EU, GFS, AROME), gratis
- **SIR Toscana** — storici osservativi validati
- **CFR Toscana** — real-time (scraping HTML, niente API)
- **ARPAT** — qualità aria
- **RainViewer** — radar precipitazioni

## Punti ancora aperti

Vedi **`docs/status.md`** sezione "Punti aperti". Quando incontri un punto aperto durante il lavoro, **fermati e segnalalo** invece di inventare una soluzione. L'utente decide.

## Struttura del repo

```
guazza/
├── CLAUDE.md               # questo file
├── AGENTS.md               # regole comportamentali Claude Code
├── config/                 # yaml configurazione
├── src/guazza/             # codice sorgente
│   ├── ingestion/          # fetcher sorgenti dati
│   ├── storage/            # DuckDB client + schema
│   ├── features/           # feature engineering
│   ├── models/             # LightGBM + CQR
│   ├── indicators/         # Decision Logic Engine
│   ├── evaluation/         # metriche, calibrazione, significatività
│   ├── output/             # JSON writer
│   └── jobs/               # entry point cron
├── scripts/                # helper Task 0 ricognizione
├── frontend-v1/            # HTML+JS statico
├── notebooks/              # esplorazione e outreach
├── deploy/                 # nginx, caddy, crontab template
├── tests/
└── docs/
    ├── status.md           # stato corrente (aggiorna l'utente)
    ├── decisions.md        # decisioni architetturali motivate
    └── known_issues.md     # problemi noti
```

## Come lavorare in questo progetto

1. **Inizia sempre leggendo `docs/status.md`** per capire dove siamo e cosa fare
2. **Un task alla volta** — non saltare avanti se il task corrente ha dipendenze non risolte
3. **Fermati sui punti aperti** — non inventare valori per soglie, coordinate, o endpoint non testati
4. **Test prima di merge** — ogni modulo ha test unitari prima di considerarsi completato
5. **Aggiorna `docs/known_issues.md`** se trovi problemi bloccanti o workaround non ovvi
6. **Suggerisci aggiornamenti a `docs/status.md`** a fine sessione

## Comandi utili

```bash
# Setup ambiente locale
uv sync

# Esegui test
uv run pytest

# Lint
uv run ruff check src/ && uv run mypy src/

# Helper Task 0
uv run python scripts/01_find_sir_stations.py
uv run python scripts/02_find_arpat_stations.py
uv run python scripts/03_probe_data_sources.py
uv run python scripts/04_check_benchmark_scrapers.py

# Ingestion manuale (dopo Sprint 1)
uv run python -m guazza.jobs.ingest_forecasts --location casa_campi

# Validazione schema DuckDB
uv run python -m guazza.storage.duckdb_client --verify-schema
```
