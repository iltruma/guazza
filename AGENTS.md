# Guazza — Istruzioni di progetto

> **STOP — prima di qualsiasi azione leggi `docs/status.md` integralmente.**
> Non scrivere codice, non fare ricerche, non proporre nulla finché non hai letto lo stato corrente.
> Vale per ogni sessione, anche breve.

Questo file è la **fonte unica** delle istruzioni di progetto. Il repo è gestito con agenti AI (Claude, Gemini, DeepSeek). Materiale
di riferimento pesante scaricato on-demand:
- `docs/contract.md` — contract JSON di output + logging DLE
- `docs/status.md` — stato corrente (unica fonte di verità, punti aperti `🟡`)
- `docs/decisions.md` — decisioni scientifiche in dettaglio
- `docs/known_issues.md` — workaround non ovvi (KI risolti in `docs/archive/known_issues_resolved.md`)
- `DESIGN.md` — design system frontend (librerie CDN in §7)
- `README.md` — albero directory completo, lista comandi operativi, struttura repo
- `config/locations.yaml` — 6 location: coordinate, stazioni SIR primarie/secondarie, upstream pluvio

## Progetto

**Guazza** — post-processing ML iper-locale per microclimi toscani. 6 location, LightGBM quantile su NWP multi-modello + osservazioni SIR.

**Tesi**: le previsioni pubbliche sbagliano sistematicamente sui microclimi specifici. Dimostrarlo con dati, fare meglio, ammettere onestamente dove si fallisce.

Duplice output: strumento personale operativo + case study pubblicabile (articolo con metodologia rigorosa, repo pubblico).

## Utente

Cloud Architect e Solution Architect con background ML applicato. Programmatore esperto (Python, infrastructure cloud, CFD, Klipper). Non ha bisogno di spiegazioni elementari.

- Risposte dirette e concrete
- Niente preamble, riepiloghi finali, filler
- Una sola domanda per turno se serve chiarimento
- Non spiegare cose ovvie
- Preferisce SQL diretto su DuckDB per query ad-hoc

## Stack blindato

Scelte validate da debate multi-modello. **Non proporre alternative** a meno che un bug tecnico reale le imponga. Tabella completa (13 voci) → `README.md` §Architettura.

| Componente | Scelta | Vincolo operativo |
|---|---|---|
| Server | NixOS baremetal (homelab astra, host `nebula`); Guazza è un tenant k3s | — |
| Scheduling | cron Linux o k8s CronJob | CLI idempotenti, orchestrator-agnostic |
| Storage analitico | DuckDB | Single-writer, PVC RWO su k8s |
| ML core | LightGBM quantile | — |
| CI calibrazione | CQR (Romano 2019) | Stratificato per lead time bucket |

### Anti-pattern — non proporre mai

- Coolify, Portainer, o qualsiasi PaaS layer che astragga l'app
- Prefect, Dagster, Airflow, Celery accoppiati alla logica applicativa (l'app deve restare orchestrator-agnostic)
- PostgreSQL, MySQL, Redis, MongoDB
- GitHub Actions come orchestratore runtime di job
- Deep learning come modello core (confronto benchmark OK, core no)
- Raspberry Pi in produzione
- Streamlit o Gradio per il frontend
- FastAPI come processo 24/7 per single user

Se uno di questi appare come dipendenza necessaria, segnalarlo e proporre alternativa conforme allo stack.

(Divieti scientifici → §Decisioni scientifiche — blindate)

**Invariante deploy**: i job sono CLI idempotenti invocabili da qualsiasi scheduler (cron, k8s CronJob, systemd). L'anti-pattern vieta l'*accoppiamento* dell'app a un orchestratore, non il *target* di deploy. Dettagli vincoli k8s → `README.md §Deploy`.

## Decisioni scientifiche — blindate

Hard stop operativi (vedi dettaglio in `docs/decisions.md`, D-001..D-022):

- **ERA5 mai come predittore di forecast** (solo climatologia statica o ground truth alternativo)
- **Embargo ≥ 7 giorni in CV** (autocorrelazione sinottica)
- **Niente valori puntuali nudi** (ogni previsione è una distribuzione con CI)

Se devi proporre un cambiamento a una di queste, prima leggi `docs/decisions.md`.

## Sorgenti dati

- **Open-Meteo Forecast + Historical Forecast API** — 4 modelli NWP: ECMWF IFS, ICON-EU, AROME France, ICON-2I (2.2km, assimila osservazioni italiane).
- **SIR Toscana** — storici osservativi validati. 34 stazioni: 21 operative, 13 upstream pluvio (ring features)
- **RainViewer** — radar precipitazioni (solo frontend, Sprint 7)

## Guardrail operativi

### 🔴 Zona rossa — mostrare e aspettare conferma

```
Rete:        qualsiasi chiamata HTTP reale (API, scraping, download)
Database:    scritture DuckDB (INSERT, UPDATE, DELETE, ALTER, migrazioni)
File dati:   scrittura su /var/lib/guazza/, config/*.yaml, Parquet esistenti
Dipendenze:  installazione pacchetti non in pyproject.toml
```

### 🟢 Zona verde — procedere autonomamente

- Scrivere/modificare codice in `src/`, `tests/`
- Leggere qualsiasi file di codice/config/docs
- Eseguire pytest, ruff, mypy
- Scrivere/aggiornare `docs/`
- `git add <file specifici>` (proporre commit, attendere conferma — vedi §Regole di commit)

## Regole di modifica del codice

### Minimal modification doctrine
La modifica minima che risolve il problema è l'unica corretta. Se un bug si risolve in 3 righe, farne 4 è un fallimento.

- Non toccare codice adiacente non richiesto: no rinomine "di passaggio", no riformattazioni, no riordino import.
- Refactoring e cleanup sono task separati, richiesti esplicitamente. Non mescolarli a fix o feature.
- Se durante il lavoro noti codice migliorabile fuori scope: segnalalo in una riga, non modificarlo.

### Gate analisi funzionale (modifiche strutturali)
Per modifiche che toccano >1 file o alterano logica esistente, prima di scrivere codice:

1. **Funzione attuale** — cosa fa oggi il modulo/funzione: input → trasformazione → output, side effect.
2. **Piano** — file toccati, cosa cambia in ciascuno, perché in quel punto.
3. **Attendere conferma** prima di scrivere.

Salta il gate solo per: fix a file singolo senza cambi di firma/contratto; istruzione esplicita "vai"/"implementa direttamente".

### Gold standard files (pattern di riferimento)
Quando scrivi nuovo codice, usa questi file come riferimento per i pattern consolidati:
- `src/guazza/fetch_openmeteo.py` — fetcher + retry + `_log_scrape` pattern
- `src/guazza/_logging.py` — `setup_logging()` pattern
- `src/guazza/features.py` — SQL-first design pattern
- `tests/test_models.py` — test pattern per suite ML

## Regole di commit

Regole complete di questo progetto (nessun file globale esterno):

- **Lingua del messaggio: italiano** — override della regola globale (che vuole i commit
  in inglese). Codice e documentazione restano comunque in inglese.
- **Formato con scope**: `<tipo>(<scope>): <descrizione>` — scope = modulo/componente
  (`ingestion`, `storage`, `indicators`, `config`, `frontend`).
- **Tipi aggiuntivi** oltre a quelli globali: `test`, `config`.
- **Non committare** dati grezzi (`*.parquet`, `*.db`) e output temporanei, oltre a quanto
  già vietato globalmente.
- **Commit**: l'agente può committare dopo aver proposto (messaggio + stat). Nessuna
  attesa di conferma esplicita per il commit stesso.
- **Push**: mai dall'agente.

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

## Procedure di sync (matrice evento → write primario)

**Regola anti-fanout** (la più importante):

- storia → `CHANGELOG.md`
- perché → `docs/decisions.md`
- workaround vivo → `docs/known_issues.md`
- coda/next → `docs/status.md`
- contratto → `docs/contract.md`
- istruzioni agenti → questo file

Mai copiare la stessa tabella in due file.

| Evento | Obbligatorio | Condizionale (solo se…) |
|---|---|---|
| feat/fix codice | `CHANGELOG.md` → `[Unreleased]` | `contract.md` se JSON; `known_issues` se workaround nuovo; `decisions` se scelta irreversibile nuova; riga Architettura in `status` se cambia "cosa c'è in prod" |
| fine sessione | `status.md` header + coda (tick/add/close P) | — |
| release/tag | `pyproject.toml` + promote CHANGELOG + cella versione in `status` | `README` solo se cambiano albero/comandi pubblici |
| chiudi KI | move → `archive/known_issues_resolved.md` | — |
| nuova decisione | append `D-NNN` in `decisions.md` | `AGENTS.md` solo se cambia invariante stack/scienza/anti-pattern |
| UI system / brand | `DESIGN.md` / `PRODUCT.md` | mai `status`, mai `CHANGELOG` salvo release note user-facing |
| doc-only | il file toccato | `CHANGELOG` voce `Docs` opzionale, non obbligatoria per typo |

**Anti-pattern**: NON aggiornare `README.md` / `PRODUCT.md` / `DESIGN.md` salvo trigger esplicito (cambia UX repo, identità prodotto, design system). Il fan-out a 7 file per ogni feature è il principale fonte di drift.

## Come lavorare

1. **Leggere `docs/status.md`** a inizio sessione
2. **Fermarsi sui punti aperti** — non inventare valori per soglie, coordinate, o endpoint non testati. Segnalare e proporre un default. Aspettare conferma se bloccante.
3. **Aggiornare `docs/known_issues.md`** se si trovano workaround non ovvi
4. **Suggerire aggiornamento a `docs/status.md`** a fine sessione
5. **Protocollo fine sessione** — riepilogo breve in 3 punti: **Fatto** (file, commit, tag) · **Non fatto / Bloccato** · **Prossimo suggerito**. Non ripetere dettagli già nel commit o nel codice.

Per modifiche strutturali: vedi §Regole di modifica del codice — Gate analisi funzionale.

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

**Scraper fragili**: `try/except` con `tenacity` exponential backoff (3 tentativi,
delay 60s/300s/600s); ping Healthchecks.io a fine run riuscito; se fallisce dopo tutti i
retry: log ERROR, ping fail, non crashare — il prossimo cron riprova.

**Decision Logic Engine**: ogni invocazione produce log in DuckDB (`indicator_log`) — schema in `docs/contract.md`.

## Comandi e ambiente

### Variabili d'ambiente rilevanti

| Variabile | Default prod | Default locale |
|---|---|---|
| `DB_PATH` | `/var/lib/guazza/guazza.duckdb` | `data/guazza.duckdb` (flag `--db`) |
| `MODEL_DIR` | `/var/lib/guazza/models` | `data/models` (flag `--model-dir`) |
| `OUTPUT_DIR` | `/var/lib/guazza/output` | `data/output` (flag `--output-dir`) |
| `HEALTHCHECKS_URL` | URL Healthchecks.io | non impostata → ping saltato |
| `CONFIG_DIR` | `{repo}/config` | auto-rilevato da `__file__` |

