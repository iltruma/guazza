# Guazza — Istruzioni di progetto

> **STOP — prima di qualsiasi azione leggi `docs/status.md` integralmente.**
> Non scrivere codice, non fare ricerche, non proporre nulla finché non hai letto lo stato corrente.
> Vale per ogni sessione, anche breve.

Questo file è la **fonte unica** delle istruzioni di progetto. Il repo è gestito con agenti AI (Claude, Gemini, DeepSeek). Materiale
di riferimento pesante scaricato on-demand:
- `docs/contract.md` — contract JSON di output + logging DLE
- `docs/frontend.md` — librerie client-side CDN (design completo in `DESIGN.md`)
- `docs/status.md` — stato corrente (unica fonte di verità, punti aperti `🟡`)
- `docs/decisions.md` — decisioni scientifiche in dettaglio
- `docs/known_issues.md` — workaround non ovvi
- `README.md` — albero directory completo e lista comandi operativi con tutti i flag

## Progetto

**Guazza** è un sistema ML di post-processing meteo iper-locale per microclimi toscani. Duplice scopo:

1. **Strumento personale** — previsioni affinate per 6 location con indicatori operativi diretti (panni, motorino, gelata, ecc.)
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

## Le 6 location

Vedi `config/locations.yaml` — coordinate complete, stazioni SIR primarie e secondarie,
stazioni ARPAT, stazioni upstream pluvio per ogni location.

## Stack blindato

Scelte validate da debate multi-modello. **Non proporre alternative** a meno che un bug tecnico reale le imponga.

| Componente | Scelta | Motivazione |
|---|---|---|
| Server | Dell Optiplex Micro 3050 — host Proxmox (homelab multi-servizio) | Hardware già disponibile, costo zero; Guazza è un tenant tra altri |
| OS | Ubuntu 24.04 LTS | LTS, standard |
| Scheduling | cron Linux o k8s CronJob | Job = CLI idempotenti orchestrator-agnostic; scheduler a scelta del homelab |
| Storage analitico | DuckDB | Column-oriented, file singolo, backup = cp |
| Storage raw NWP | Parquet partizionato | Compresso, leggibile con pandas/polars |
| Backup | Cloudflare R2 (10GB free) | Egress gratis, free tier |
| ML core | LightGBM quantile | Gold standard dati tabulari, no GPU |
| CI calibrazione | CQR (Romano 2019) | Garanzia copertura marginale |
| Esposizione | Cloudflare Tunnel (cloudflared) | Nessun IP pubblico, no port forwarding, SSL automatico |
| Deploy | CI su GitHub Actions (pubblica); CD nel homelab (es. namespace k8s) | CI clean-room + badge; il deploy non vincola l'app |
| Frontend | HTML + CSS custom + Chart.js + Leaflet + Nginx | Statico, CSS custom (no framework), librerie e font via CDN jsDelivr |
| DNS/CDN/WAF | Cloudflare | Gratis |
| Monitoring | Healthchecks.io + UptimeRobot | Free tier, dead-man switch |
| Retry scraper | tenacity | Exponential backoff, standard |
| Logging | loguru | JSON strutturato |
| Validation | pydantic v2 | Solo ai boundary: config YAML in ingresso, JSON in uscita |
| HTTP | httpx (sync) | Niente async overhead per cron |

### Anti-pattern — non proporre mai

- Coolify, Portainer, o qualsiasi PaaS layer che astragga l'app
- Prefect, Dagster, Airflow, Celery accoppiati alla logica applicativa (l'app deve restare orchestrator-agnostic)
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

**Invariante deploy**: i job sono CLI idempotenti invocabili da qualsiasi scheduler
(cron, k8s CronJob, systemd). Deployare Guazza come namespace k8s con DB in PVC è una
scelta legittima del homelab — l'anti-pattern vieta l'*accoppiamento* dell'app a un
orchestratore, non il *target* di deploy. Vincoli tecnici se si va su k8s: DuckDB è
single-writer (`concurrencyPolicy: Forbid` sui CronJob writer), PVC `ReadWriteOnce` su
storage local-path (`flock` inaffidabile su NFS/Ceph), backup `cp`/snapshot in un CronJob
dedicato.

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

- **Open-Meteo Forecast + Historical Forecast API** — 6 modelli NWP: ECMWF IFS, ICON-EU, ICON-D2 (2.2km), GFS 0.25°, AROME France, ICON-2I (2.2km, assimila osservazioni italiane). `ecmwf_aifs025` rimosso: restituisce null su tutte le variabili.
- **SIR Toscana** — storici osservativi validati. 34 stazioni: 21 operative, 13 upstream pluvio (ring features)
- **ARPAT** — qualità aria (NO2, O3, CO, SO2 orari NRT; PM10, PM2.5, benzene giornalieri da bollettini)
- **RainViewer** — radar precipitazioni (solo frontend, Sprint 7)

## Struttura repo

Struttura **flat** — un file per modulo in `src/guazza/`, no package annidati.
L'albero completo delle directory è in `README.md`. Mappa dei moduli (responsabilità):

- `schema.sql` — schema DuckDB (unico source of truth; include la vista `obs_weighted_daily`)
- `storage.py` — DuckDBClient, upsert_*, backfill_prediction_obs
- `fetchers.py` — CLI fetcher; la logica è nei moduli `fetch_*` per dominio:
  - `fetch_common.py` — costanti/helper HTTP condivisi · `fetch_sir.py` — SIR ·
    `fetch_openmeteo.py` — Open-Meteo · `fetch_netatmo.py` — Netatmo · `fetch_arpat.py` — ARPAT
- `_paths.py` — path di default da env (DB_PATH, CONFIG_DIR, OUTPUT_DIR)
- `weights.py` — pesi stazione→location, refresh_upstream_rings()
- `features.py` — build_features_daily() → tabella features_daily
- `models.py` — LightGBM quantile + CQR, train_all(), predict()
- `indicators.py` — Decision Logic Engine, evaluate_all(), log_results()
- `output.py` — build_signals(), compute_coverage_30d(), write_location_json()
- `qc.py` — quality control osservazioni SIR + ARPAT (chiamato da ingest post-upsert)
- `_logging.py` — setup_logging() (TTY pretty / cron JSON)
- `netatmo_daily.py` — accumulo Netatmo realtime → daily (forward-looking storico)
- `jobs/` — entrypoint CLI cron: ingest, features, train, predict, backup, skill, skill_history, monitor

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
- Leggere qualsiasi file di codice/config/docs (i dati pesanti e i segreti sono esclusi dal `.claudeignore` — per ispezionarli usare Bash)
- Eseguire pytest, ruff, mypy
- Scrivere/aggiornare `docs/`
- `git add <file specifici>` + `git commit` (mai `git push`)

### Errori in esecuzione

Quando uno script va in errore:
1. Fermarsi immediatamente — non tentare fix autonomi, non ritentare varianti
2. Mostrare l'errore esattamente com'è (output completo)
3. Aspettare istruzioni

Non fare reverse engineering autonomo su API o sistemi esterni quando uno script fallisce.

## .claudeignore

Il repo ha un `.claudeignore` che tiene fuori dal contesto di Claude i blob pesanti
(`data/`, `*.duckdb`, `models/`, `*.parquet`), gli artefatti rigenerabili e i segreti.
Replica il `.gitignore` con tre differenze intenzionali:

- **`.env.example` resta leggibile** (`!.env.example`) — i segreti veri (`.env`) no
- **`.git/` e `uv.lock` esclusi** — leggibili da git ma solo rumore per il modello
- per il resto i due file vanno tenuti allineati: a ogni modifica del `.gitignore`, verificare il `.claudeignore`

Conseguenza: i file in `data/` (output JSON, DuckDB) **non** sono leggibili via Read/Grep.
Per ispezionarli usare Bash (`jq`/`cat`).

## Regole di commit

Valgono le **Git Commit Guidelines globali** (`~/.config/opencode/AGENTS.md`: formato, regole,
staging selettivo, atomicità). Override e aggiunte specifiche di questo progetto:

- **Lingua del messaggio: italiano** — override della regola globale (che vuole i commit
  in inglese). Codice e documentazione restano comunque in inglese.
- **Formato con scope**: `<tipo>(<scope>): <descrizione>` — scope = modulo/componente
  (`ingestion`, `storage`, `indicators`, `config`, `frontend`).
- **Tipi aggiuntivi** oltre a quelli globali: `test`, `config`.
- **Non committare** dati grezzi (`*.parquet`, `*.db`) e output temporanei, oltre a quanto
  già vietato globalmente.
- **Conferma obbligatoria**: non committare mai in autonomia, aspettare sempre conferma
  esplicita dell'utente.

### Commit: proporre al completamento di ogni task

Trigger: task completato con test verdi e lint pulito; aggiornamento `docs/status.md` o
`docs/known_issues.md`; aggiunta/modifica di `config/*.yaml`; milestone intermedia stabile
(schema DuckDB, primo fetcher funzionante). Preferire commit atomici (un task = un commit;
se ha sotto-step, un commit per sotto-step).

### Tag versione + CHANGELOG — fine sessione significativa

Alla fine di una sessione che include almeno uno tra: nuova location/sorgente/modello NWP;
nuovo modulo o feat in `src/guazza/`; frontend redesign o modifica al JSON contract;
refactoring che tocca schema DuckDB o rimuove codice obsoleto; 5+ commit feat/fix —
**chiedere all'utente** se serve bump versione + CHANGELOG. Aggiornare sempre insieme:
`pyproject.toml` (version), `CHANGELOG.md`, `docs/status.md` (header data), `README.md`
(roadmap/comandi/struttura se cambiati). Non eseguire bump/tag senza conferma esplicita.
Saltare la proposta per: fix banali, lint, test, refactor interni, doc fix, sessioni di
soli chore/dipendenze.

### Push e hook

- `git push` non va **mai** eseguito. L'utente gestisce il push manualmente.
- Se un pre-commit hook fallisce: non usare `--no-verify`. Fermarsi, mostrare l'errore, aspettare istruzioni.

## Come lavorare

1. **Leggere `docs/status.md`** a inizio sessione
2. **Risposte dirette, senza preambolo** — niente "Certo! Ecco come possiamo procedere...",
   niente riepilogo di ciò che hai appena fatto, niente spiegazioni ovvie. Dai subito la
   risposta o proponi subito il piano. Se il codice è nei file, non ripeterlo nel testo.
3. **Un task alla volta** — non saltare avanti se ci sono dipendenze non risolte
4. **Fermarsi sui punti aperti** — non inventare valori per soglie, coordinate, o endpoint non testati. Segnalare e proporre un default. Aspettare conferma se bloccante.
5. **Test prima di considerare completato** — almeno happy path + edge case principale
6. **Codice tipato** — type hints ovunque, mypy deve passare. `pydantic v2` solo ai
   boundary di sistema (validazione config YAML in ingresso, JSON di output verso il
   frontend); `@dataclass` per gli oggetti interni fidati — non validare codice interno
7. **Aggiornare `docs/known_issues.md`** se si trovano workaround non ovvi
8. **Suggerire aggiornamento a `docs/status.md`** a fine sessione
9. Non assumere che l'ambiente sia pulito — verificare che i test passino prima di nuove feature
10. **Protocollo fine sessione** — riepilogo breve (3-5 righe) in 3 punti: **Fatto** (file,
    commit, tag) · **Non fatto / Bloccato** (cosa è rimasto e perché) · **Prossimo suggerito**
    (un passo logico). Non ripetere dettagli già nel commit o nel codice.

### Spiegazione obbligatoria prima di modificare file

Per ogni modifica non banale a qualsiasi file del progetto (codice, docs, config YAML,
README), spiegare e **aspettare conferma**:

1. **Cosa cambia** — il problema tecnico concreto che la modifica risolve (non astratto)
2. **Come** — le scelte implementative rilevanti: cosa hai considerato, cosa hai scartato e perché
3. **Impatto** — cosa cambia per chi legge o modifica il codice dopo

L'obiettivo non è validare la scelta, ma permettere all'utente di capire, valutare e
mantenere il codice in autonomia.

Eccezioni (procedere direttamente): fix banali (typo, 1-2 righe senza effetti collaterali);
istruzione esplicita "vai"/"implementa direttamente"; correzioni lint/mypy/test che non
cambiano semantica; `docs/known_issues.md` e `docs/status.md` a fine sessione (routine — ma
comunicare cosa si scrive prima di farlo).

## Logging — regole obbligatorie

**Setup**: ogni job CLI deve chiamare `setup_logging()` da `guazza._logging` prima
di emettere qualsiasi log. Mai `print()` nei job; mai `logger.add()` diretto fuori
da `_logging.py`. Comportamento automatico:
- TTY interattivo → formato colorato human-readable su stderr (`HH:mm:ss | LEVEL | messaggio`)
- Cron / pipe (non-TTY) → JSON strutturato su stdout, una riga per evento

**Pattern `_log_scrape`**: ogni fetcher emette `_log_scrape("<sorgente>:<id>", "ok"|"fail", rows=N)`
sia su successo sia su fallimento — è l'unico log machine-readable per evento di scraping.
Chiave formato `<sorgente>:<identificatore>`, senza suffissi `_batch`. Dove c'è `_log_scrape`,
il `logger.info` discorsivo non ripete le stesse informazioni (es. conteggio righe già in `rows=`).

**Scraper fragili (ARPAT)**: `try/except` con `tenacity` exponential backoff (3 tentativi,
delay 60s/300s/600s); ping Healthchecks.io a fine run riuscito; se fallisce dopo tutti i
retry: log ERROR, ping fail, non crashare — il prossimo cron riprova.

**Decision Logic Engine**: ogni invocazione produce log in DuckDB (`indicator_log`) — schema in `docs/contract.md`.

## Qualità del codice

- **Leggibile da un mid developer**: nomi espliciti (no abbreviazioni criptiche),
  funzioni corte con un solo scopo, niente magie implicite non ovvie.
- **Niente dead code**: funzioni, variabili, import non usati si rimuovono subito.
  Non lasciare codice "forse utile in futuro".
- **Niente codice sperimentale residuo**: niente `print()` di debug, variabili temporanee,
  rami commentati. Si elimina prima del commit.
- **Niente helper prematuri**: tre righe simili non giustificano un'astrazione.
  Estrarre una funzione solo quando il riuso è concreto e immediato.
- **Commenti solo sul "perché"**: non sul "cosa" (il codice lo dice già). Un commento che
  descrive ciò che fa la riga è rumore — va rimosso.

### Checklist completamento task

- [ ] Codice tipato, mypy passa
- [ ] Test pytest scritti e verdi (`uv run python -m pytest`)
- [ ] `ruff check` passa (zero warning)
- [ ] `setup_logging()` chiamato in ogni nuovo job CLI
- [ ] Healthchecks.io ping se è un job cron
- [ ] Nessun dead code, nessun codice sperimentale residuo
- [ ] Punto aperto segnalato se il task ne dipende
- [ ] `docs/known_issues.md` aggiornato se workaround
- [ ] Versione/CHANGELOG proposti se la sessione lo richiede
- [ ] Commit proposto con formato `tipo(scope): descrizione`

## Comandi e ambiente

Comandi di sviluppo più usati (lista operativa completa con tutti i flag in `README.md`):

```bash
uv sync --extra dev                          # ambiente + dev deps
uv run python -m pytest                       # test
uv run ruff check src/ && uv run mypy src/   # lint + type check
```

### Variabili d'ambiente rilevanti

| Variabile | Default prod | Default locale |
|---|---|---|
| `DB_PATH` | `/var/lib/guazza/guazza.duckdb` | `data/guazza.duckdb` (flag `--db`) |
| `MODEL_DIR` | `/var/lib/guazza/models` | `data/models` (flag `--model-dir`) |
| `OUTPUT_DIR` | `/var/lib/guazza/output` | `data/output` (flag `--output-dir`) |
| `HEALTHCHECKS_URL` | URL Healthchecks.io | non impostata → ping saltato |
| `CONFIG_DIR` | `{repo}/config` | auto-rilevato da `__file__` |

## Ottimizzazione costi (token routing)

- **Batchare tool calls** in ogni turno quando possibile (chiamate indipendenti in parallelo)
- **Mai loop di ricerca** su API o scraping senza limite; 3 tentativi max con backoff
- **Preferire Read/Grep** a sub-agenti per ricerche in <5 file
- **Non riscrivere file interi** se Edit può modificare il blocco rilevante
- **Commit atomici** a ogni milestone stabile per evitare perdita lavoro
- **Evitare risposte lunghe** — codice nei file, non nel testo della chat
