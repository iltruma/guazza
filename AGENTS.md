# Guazza — Istruzioni di progetto

> **STOP — prima di qualsiasi azione leggi `docs/status.md` integralmente.**
> Non scrivere codice, non fare ricerche, non proporre nulla finché non hai letto lo stato corrente.
> Questo vale per ogni agente (Claude, Gemini, GPT, o altro) e per ogni sessione, anche breve.

## Progetto

**Guazza** è un sistema ML di post-processing meteo iper-locale per microclimi toscani. Duplice scopo:

1. **Strumento personale** — previsioni affinate per 4 location con indicatori operativi diretti (panni, motorino, gelata, ecc.)
2. **Case study pubblicabile** — articolo LinkedIn/Medium con metodologia rigorosa, bibliografia scientifica, repo pubblico

**Tesi**: le previsioni pubbliche (ECMWF, LAMMA, 3BMeteo, ecc.) sbagliano sistematicamente sui microclimi specifici. Dimostrarlo con dati e fare meglio, ammettendo onestamente dove si fallisce.

Progetto personale con ambizioni scientifiche, sviluppato in spare time a sprint irregolari su orizzonte 12-18 mesi.

## Utente

Cloud Architect e Solution Architect con background ML applicato. Programmatore esperto (Python, infrastructure cloud, CFD, Klipper). Non ha bisogno di spiegazioni elementari.

- Risposte dirette e concrete
- Niente preamble, riepiloghi finali, filler
- Una sola domanda per turno se serve chiarimento
- Non spiegare cose ovvie
- Preferisce SQL diretto su DuckDB per query ad-hoc

## Stato corrente

Leggi **`docs/status.md`** — unica fonte di verità sullo stato. Punti aperti con tag `🟡`.

## Le 4 location

```yaml
casa_campi:      # Campi Bisenzio (FI), ~35m
lavoro_cosimo:   # Scandicci (FI), ~50m
lavoro_madda:    # Prato, ~60m
casa_cesto:      # Figline Valdarno (FI), ~200m
```

Coordinate complete in `config/locations.yaml`.

## Stack blindato

Scelte validate da debate multi-modello. **Non proporre alternative** a meno che un bug tecnico reale le imponga.

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

## Anti-pattern — non proporre mai

- Coolify, Portainer, o qualsiasi PaaS layer
- Prefect, Dagster, Airflow, Celery come orchestratori
- Kubernetes, Docker Swarm, ArgoCD
- PostgreSQL, MySQL, Redis, MongoDB
- GitHub Actions come orchestratore runtime di job
- ERA5 come predittore di forecast (solo come climatologia statica)
- Embargo < 7 giorni nella cross-validation
- Valori puntuali nudi senza confidence interval
- Deep learning come modello core (confronto benchmark OK, core no)
- Raspberry Pi in produzione
- Streamlit o Gradio per il frontend
- FastAPI come processo 24/7 per single user

Se uno di questi appare come dipendenza necessaria, segnalarlo e proporre alternativa conforme allo stack.

## Decisioni scientifiche — blindate

Dettaglio completo in `docs/decisions.md`. Sintesi:

- **ERA5 mai come predittore di forecast** — solo climatologia statica o ground truth alternativo. Usarlo come predittore = train/serve skew grave.
- **Embargo 7 giorni in CV** — autocorrelazione sinottica. Meno = metriche gonfiate.
- **CQR stratificato per lead time bucket** — calibration set separato per: 0-6h, 6-12h, 12-24h, 24-48h, 48-72h
- **`coverage_empirical_30d` nel JSON di output** — onestà su quanto fidarsi del CI
- **Modello globale con location-id categorica** — no modelli per-location indipendenti
- **Ogni previsione è una distribuzione** — mai valori puntuali nudi senza CI
- **Indicatori operativi sono il prodotto** — non feature secondarie

### ERA5 — regola critica

ERA5 è una reanalisi che assimila osservazioni reali. Usarlo come predittore introduce train/serve skew perché in produzione si usano forecasts che non hanno visto la verità.

Usi consentiti:
- Features climatologiche statiche (media/std mensile multi-decennale)
- Ground truth alternativo per location senza stazioni SIR
- Backfill storico solo come target (osservazione), mai come predittore

Se ERA5 appare come input dinamico a un modello: **è un bug**.

## Sorgenti dati

- **Open-Meteo Forecast + Historical Forecast API** — multi-modello (ECMWF, ICON-EU, GFS, AROME), gratis
- **SIR Toscana** — storici osservativi validati
- **CFR Toscana** — real-time (scraping HTML, niente API)
- **ARPAT** — qualità aria
- **RainViewer** — radar precipitazioni

## Struttura repo

```
guazza/
├── AGENTS.md               # istruzioni di progetto (source of truth)
├── CLAUDE.md               # override Claude Code (importa @AGENTS.md)
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
├── deploy/                 # nginx, caddy, crontab template
├── tests/
└── docs/
    ├── status.md           # stato corrente
    ├── decisions.md        # decisioni architetturali motivate
    └── known_issues.md     # problemi noti
```

## Guardrail operativi

### 🔴 Zona rossa — mostrare e aspettare conferma

```
Rete:        qualsiasi chiamata HTTP reale (API, scraping, download)
Database:    scritture DuckDB (INSERT, UPDATE, DELETE, ALTER, migrazioni)
File dati:   scrittura su /var/lib/guazza/, config/*.yaml, Parquet esistenti
Dipendenze:  installazione pacchetti non in pyproject.toml
```

Pattern obbligatorio prima di procedere:
```
Sto per eseguire:
  [tipo]: [dettaglio esatto — URL, query SQL, comando]
  Scopo: [perché è necessario]
  Impatto: [cosa cambia/scrive/modifica]

Attendo conferma prima di procedere.
```

### 🟢 Zona verde — procedere autonomamente

- Scrivere/modificare codice in `src/`, `tests/`
- Leggere qualsiasi file
- Eseguire pytest, ruff, mypy
- Scrivere/aggiornare `docs/`
- `git add <file specifici>` + `git commit` (mai `git push`)

### Errori in esecuzione

Quando uno script va in errore:
1. Fermarsi immediatamente — non tentare fix autonomi, non ritentare varianti
2. Mostrare l'errore esattamente com'è (output completo)
3. Aspettare istruzioni

Non fare reverse engineering autonomo su API o sistemi esterni quando uno script fallisce.

## Regole di commit

### Commit: autonomo al completamento di ogni task

Trigger obbligatori:
- Task completato con test verdi e lint pulito
- Aggiornamento di `docs/status.md` o `docs/known_issues.md`
- Aggiunta/modifica di configurazione (`config/*.yaml`)
- Milestone intermedia stabile (schema DuckDB, primo fetcher funzionante)

**Formato messaggio:**
```
<tipo>(<scope>): <descrizione breve in italiano>

[corpo opzionale se serve contesto]
```
Tipi: `feat`, `fix`, `test`, `docs`, `config`, `refactor`, `chore`
Scope: modulo o componente (`ingestion`, `storage`, `indicators`, `config`)

**Staging selettivo** — aggiungere solo i file pertinenti al task. Mai `git add -A` o `git add .` cieco.

**Non committare mai:** `.env`, file con credenziali, dati grezzi (`*.parquet`, `*.db`), output temporanei.

### Push: vietato incondizionatamente

`git push` non va mai eseguito. L'utente gestisce il push manualmente.

### Hook

Se un pre-commit hook fallisce: non usare `--no-verify`. Fermarsi, mostrare l'errore, aspettare istruzioni.

Preferire commit atomici (un task = un commit). Se il task ha sotto-step, un commit per sotto-step.

## Come lavorare

1. **Leggere `docs/status.md`** a inizio sessione
2. **Un task alla volta** — non saltare avanti se ci sono dipendenze non risolte
3. **Fermarsi sui punti aperti** — non inventare valori per soglie, coordinate, o endpoint non testati. Segnalare e proporre un default. Aspettare conferma se bloccante.
4. **Test prima di considerare completato** — almeno happy path + edge case principale
5. **Codice tipato** — type hints ovunque, mypy deve passare, pydantic v2 per modelli dati
6. **Aggiornare `docs/known_issues.md`** se si trovano workaround non ovvi
7. **Suggerire aggiornamento a `docs/status.md`** a fine sessione
8. Non assumere che l'ambiente sia pulito — verificare che i test passino prima di nuove feature

### Spiegazione obbligatoria prima di modificare file

Per ogni modifica non banale a qualsiasi file del progetto
(codice, docs, config YAML, README, AGENTS.md):

1. **Cosa cambia** — il problema che la modifica risolve
2. **Come** — approccio ad alto livello
3. **Alternative scartate** — solo se la scelta non è ovvia
4. **Aspetta conferma** prima di procedere

Eccezioni (procedere direttamente):
- Fix banali: typo, 1-2 righe senza effetti collaterali
- Task con istruzione esplicita "vai" o "implementa direttamente"
- Correzioni lint/mypy/test che non cambiano semantica
- `docs/known_issues.md` e `docs/status.md` a fine sessione
  (aggiornamento di routine — ma comunicare cosa si sta scrivendo prima di farlo)

### Scraper fragili (CFR Toscana, ARPAT)

- `try/except` con `tenacity` exponential backoff (3 tentativi, delay 60s, 300s, 600s)
- Log strutturato `loguru`: ogni run → `{"scraper": "...", "status": "ok|fail", "ts": ..., "rows": N}`
- Ping Healthchecks.io a fine run riuscito
- Se fallisce dopo tutti i retry: log ERROR, ping fail, non crashare — il prossimo cron riprova

### Decision Logic Engine — logging obbligatorio

Ogni invocazione DLE deve produrre log in DuckDB (`indicator_log`):
```python
{"ts": datetime, "location_id": str, "indicator_id": str,
 "input_summary": dict, "rule_matched": str, "verdict": str, "probability": float}
```

### Output JSON — contract obbligatorio

```json
{"coverage_empirical_30d": {"temp_ci80": float, "temp_ci90": float}}
```
Rolling window 30 giorni. Se dati insufficienti → `null`, dashboard mostra "calibrazione in corso".

### Checklist completamento task

- [ ] Codice tipato, mypy passa
- [ ] Test pytest scritti e verdi
- [ ] `ruff check` passa (zero warning)
- [ ] Log strutturato dove rilevante
- [ ] Healthchecks.io ping se è un job cron
- [ ] Punto aperto segnalato se il task ne dipende
- [ ] `docs/known_issues.md` aggiornato se workaround
- [ ] Commit creato con formato `tipo(scope): descrizione`

## Comandi utili

```bash
# Setup ambiente locale
uv sync

# Esegui test
uv run pytest

# Lint
uv run ruff check src/ && uv run mypy src/

# Ingestion manuale
uv run python -m guazza.jobs.ingest historical --dry-run
uv run python -m guazza.jobs.ingest daily
uv run python -m guazza.jobs.ingest realtime
uv run python -m guazza.jobs.ingest forecasts

# Quality control
uv run python -m guazza.jobs.qc run
uv run python -m guazza.jobs.qc run --dry-run
uv run python -m guazza.jobs.qc report

# Validazione schema DuckDB
uv run python -m guazza.storage verify-schema
```

## Regole di routing modelli — ottimizzazione costi

- **Batchare tool calls** in ogni turno quando possibile (max 32 chiamate parallele)
- **Mai loop di ricerca** su API o scraping senza limite; 3 tentativi max con backoff
- **Preferire `read`/`grep`** a `task` per ricerche in <5 file
- **Non riscrivere file interi** se `edit` può modificare il blocco rilevante
- **Commit atomici** a ogni milestone stabile per evitare perdita lavoro
- **Evitare risposte lunghe** — codice nei file, non nel testo della chat
