# Guazza — Istruzioni di progetto

> **STOP — prima di qualsiasi azione leggi `docs/status.md` integralmente.**
> Non scrivere codice, non fare ricerche, non proporre nulla finché non hai letto lo stato corrente.
> Questo vale per ogni agente (Claude, Gemini, GPT, o altro) e per ogni sessione, anche breve.

## Progetto

**Guazza** è un sistema ML di post-processing meteo iper-locale per microclimi toscani. Duplice scopo:

1. **Strumento personale** — previsioni affinate per 5 location con indicatori operativi diretti (panni, motorino, gelata, ecc.)
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

## Le 5 location

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

## Anti-pattern — non proporre mai

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

- `schema.sql` — schema DuckDB (unico source of truth)
- `storage.py` — DuckDBClient, upsert_*, backfill_prediction_obs
- `fetchers.py` — SIR storico/realtime, Netatmo, Open-Meteo, ARPAT
- `weights.py` — pesi stazione→location, refresh_upstream_rings()
- `features.py` — build_features_daily() → tabella features_daily
- `models.py` — LightGBM quantile + CQR, train_all(), predict()
- `indicators.py` — Decision Logic Engine, evaluate_all(), log_results()
- `output.py` — build_signals(), compute_coverage_30d(), write_location_json()
- `qc.py` — quality control osservazioni SIR + ARPAT
- `_logging.py` — setup_logging() (TTY pretty / cron JSON)
- `jobs/` — entrypoint CLI cron: ingest, features, train, predict, qc, backup

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

Valgono le **Git Commit Guidelines globali** (`~/.claude/AGENTS.md`: formato, regole,
staging selettivo, atomicità). Override e aggiunte specifiche di questo progetto:

- **Lingua del messaggio: italiano** — override della regola globale (che vuole i commit
  in inglese). Codice e documentazione restano comunque in inglese.
- **Formato con scope**: `<tipo>(<scope>): <descrizione>` — scope = modulo/componente
  (`ingestion`, `storage`, `indicators`, `config`, `frontend`).
- **Tipi aggiuntivi** oltre a quelli globali: `test`, `config`.
- **Non committare** dati grezzi (`*.parquet`, `*.db`) e output temporanei, oltre a quanto
  già vietato globalmente.

### Commit: autonomo al completamento di ogni task

Trigger obbligatori:
- Task completato con test verdi e lint pulito
- Aggiornamento di `docs/status.md` o `docs/known_issues.md`
- Aggiunta/modifica di configurazione (`config/*.yaml`)
- Milestone intermedia stabile (schema DuckDB, primo fetcher funzionante)

### Tag versione + CHANGELOG — obbligatorio a fine sessione significativa

Alla fine di ogni sessione che include **almeno uno** di questi trigger, **chiedere
all'utente** se serve bump di versione + aggiornamento CHANGELOG:

- Nuova location, nuova sorgente dati, nuovo modello NWP
- Nuovo modulo/funzionalità (feat) in `src/guazza/`
- Frontend redesign o modifica strutturale al JSON contract
- Refactoring che modifica schema DuckDB o rimuove codice obsoleto
- 5+ commit feat/fix nella stessa sessione

Procedura:
1. Revisionare i commit della sessione e categorizzare (feat/fix/refactor/docs/chore)
2. Proporre versione e aggiornamento CHANGELOG con formato:
   ```
   Propongo v<X>.<Y>.<Z> — <motivo>. Modifiche:
   - pyproject.toml → version
   - CHANGELOG.md → nuova sezione
   - docs/status.md → aggiornamento header data
   ```
3. Non eseguire bump senza conferma esplicita
4. Dopo conferma: commit + tag annotato con messaggio descrittivo

Trigger che **non** richiedono proposta (saltare):
- Fix banali, lint, test, refactor interni senza impatto esterno
- Doc fix, readme, known_issues
- Sessione con solo chore/dipendenze

Formato proposta tag:
```
Propongo tag vX.Y.Z — <milestone raggiunta>. Confermo?
```

Non creare il tag senza conferma esplicita dell'utente.

### Push: vietato incondizionatamente

`git push` non va mai eseguito. L'utente gestisce il push manualmente.

### Hook

Se un pre-commit hook fallisce: non usare `--no-verify`. Fermarsi, mostrare l'errore, aspettare istruzioni.

Preferire commit atomici (un task = un commit). Se il task ha sotto-step, un commit per sotto-step.

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
10. **Protocollo fine sessione** — prima di terminare, fornisci un riepilogo in 3 punti:
    - **Fatto**: cosa è stato completato (file, commit, tag)
    - **Non fatto / Bloccato**: cosa è rimasto indietro e perché (punto aperto, mancata conferma)
    - **Prossimo suggerito**: un prossimo passo logico

    Il riepilogo deve essere breve (3-5 righe). Non ripetere dettagli già nel commit o nel codice.

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

### Logging — regole obbligatorie

**Setup**: ogni job CLI deve chiamare `setup_logging()` da `guazza._logging` prima
di emettere qualsiasi log. Mai `print()` nei job; mai `logger.add()` diretto fuori
da `_logging.py`. Il comportamento è automatico:
- TTY interattivo → formato colorato human-readable su stderr (`HH:mm:ss | LEVEL | messaggio`)
- Cron / pipe (non-TTY) → JSON strutturato su stdout, una riga per evento

**Pattern `_log_scrape`**: ogni fetcher emette `_log_scrape("<sorgente>:<id>", "ok"|"fail", rows=N)`
sia su successo sia su fallimento — è l'unico log machine-readable per evento di scraping.
Chiave formato `<sorgente>:<identificatore>`, senza suffissi `_batch`.

**Niente duplicati**: dove c'è `_log_scrape`, il `logger.info` discorsivo non
ripete le stesse informazioni (es. conteggio righe già in `rows=`).

### Scraper fragili (ARPAT)

- `try/except` con `tenacity` exponential backoff (3 tentativi, delay 60s, 300s, 600s)
- Ping Healthchecks.io a fine run riuscito
- Se fallisce dopo tutti i retry: log ERROR, ping fail, non crashare — il prossimo cron riprova

### Decision Logic Engine — logging obbligatorio

Ogni invocazione DLE deve produrre log in DuckDB (`indicator_log`):
```python
{"ts": datetime, "location_id": str, "indicator_id": str,
 "input_summary": dict, "rule_matched": str, "verdict": str, "probability": float}
```

### Output JSON — contract obbligatorio

File: `data/output/{location_id}.json` (uno per location, sovrascritto ad ogni run di `predict`).
Struttura multi-giorno: ogni file contiene la striscia `days` da D+0 a D+7.

```json
{
  "location_id": "casa_campi",
  "generated_at": "2026-05-18T...",
  "coverage_empirical_30d": {
    "tmin_ci80": float | null, "tmin_ci90": float | null,
    "tmax_ci80": float | null, "tmax_ci90": float | null,
    "precip_ci80": float | null, "precip_ci90": float | null
  },
  "current": {"ts": str, "temp_c": float, "humidity_pct": float, "precip_mm": float,
              "wind_speed_ms": float | null, "wind_dir_deg": float | null,
              "dewpoint_c": float, "feels_like_c": float,
              "pressure_hpa": float | null},
  "air_quality": {"pm10_ugm3": float | null, "pm25_ugm3": float | null,
                  "no2_ugm3": float | null, "o3_ugm3": float | null,
                  "co_mgm3": float | null, "benzene_ugm3": float | null,
                  "so2_ugm3": float | null},
  "nwp_models_hourly": [{"source": str, "label": str, "data": [{...}]}],
  "days": [
    {
      "target_date": "2026-05-19",
      "lead_time_h": 24,
      "forecasts": {
        "tmin_c":    {"p50": float, "ci80_lo": float, "ci80_hi": float, "ci90_lo": float, "ci90_hi": float},
        "tmax_c":    {"p50": float, ...},
        "precip_mm": {"p50": float, ...}
      },
      "indicators": {
        "panni":    {"verdict": "verde|giallo|rosso", "rule_matched": "green|yellow|red|fallback"},
        "motorino": {"verdict": "...", "rule_matched": "..."}
      },
      "hourly": [{...}],
      "nwp_comparison": [{"source": str, "label": str, "tmin_c": float,
                          "tmax_c": float, "precip_mm": float, "last_run": str}]
    }
  ]
}
```

`coverage_empirical_30d`: rolling 30 giorni predictions vs obs. `null` se < 10 campioni → dashboard mostra "calibrazione in corso".
`current` e `air_quality` sono `null` se non ci sono osservazioni recenti (rispettivamente realtime meteo e ARPAT).
`current.pressure_hpa` è la pressione di superficie da Open-Meteo (non SIR) — può essere `null` se non ci sono dati NWP recenti.

### Frontend — librerie client-side (CDN jsDelivr)

Il frontend usa **CSS custom** (`style.css`, classi prefissate `g-*`) — nessun framework
CSS (Tailwind/DaisyUI rimossi nel redesign v2, vedi `DESIGN.md`). Font caricati via CDN:
**Geist** (display/titoli) + **JetBrains Mono** (dati numerici). Librerie client-side
caricate via CDN jsDelivr: **Chart.js** (+ adapter date-fns), **Leaflet 1.9.4**, oltre a:
- **twemoji@14.0.2** — emoji Unicode convertite in SVG per consistenza cross-browser.
  `twemoji.parse(container, TWEMOJI_OPTS)` va chiamato **dopo ogni update di `innerHTML`
  nel container `#app`** e una volta sull'`header` all'init. Il fix CSS
  `img.emoji { height:1em; width:1em; vertical-align:-0.1em; }` in `style.css` allinea gli
  SVG al testo circostante.
- **suncalc** — alba/tramonto (`SunCalc.getTimes`) e fase lunare
  (`SunCalc.getMoonIllumination().phase`) calcolati client-side dalle coordinate location.
  Fase lunare: 8 emoji (🌑→🌘) con tooltip nome in italiano. Aurora/crepuscolo civile
  non mostrati.

### Qualità del codice

- **Leggibile da un mid developer**: nomi espliciti (no abbreviazioni criptiche),
  funzioni corte con un solo scopo, niente magie implicite non ovvie.
- **Niente dead code**: funzioni, variabili, import non usati si rimuovono subito.
  Non lasciare codice "forse utile in futuro".
- **Niente codice sperimentale residuo**: se qualcosa è stato scritto per una prova
  o debug e non è più necessario, si elimina prima del commit. Non committare
  `print()` di debug, variabili temporanee, rami commentati.
- **Niente helper prematuri**: tre righe simili non giustificano un'astrazione.
  Estrarre una funzione solo quando il riuso è concreto e immediato.
- **Commenti solo sul "perché"**: non sul "cosa" (il codice lo dice già).
  Un commento che descrive ciò che fa la riga è rumore — va rimosso.

### Checklist completamento task

- [ ] Codice tipato, mypy passa
- [ ] Test pytest scritti e verdi
- [ ] `ruff check` passa (zero warning)
- [ ] `setup_logging()` chiamato in ogni nuovo job CLI
- [ ] Healthchecks.io ping se è un job cron
- [ ] Nessun dead code, nessun codice sperimentale residuo
- [ ] Punto aperto segnalato se il task ne dipende
- [ ] `docs/known_issues.md` aggiornato se workaround
- [ ] Versione e CHANGELOG proposti se la sessione lo richiede (vedi "Tag versione + CHANGELOG")
- [ ] Commit creato con formato `tipo(scope): descrizione`

## Comandi utili

Comandi di sviluppo più usati:

```bash
uv sync --extra dev                          # ambiente + dev deps
uv run pytest                                # test
uv run ruff check src/ && uv run mypy src/   # lint + type check
```

L'elenco completo dei comandi operativi (ingestion, features, weights, train,
predict, qc, schema) con tutti i flag e le varianti è in `README.md`.

### Variabili d'ambiente rilevanti

| Variabile | Default prod | Default locale |
|---|---|---|
| `DB_PATH` | `/var/lib/guazza/guazza.duckdb` | `data/guazza.duckdb` (flag `--db`) |
| `MODEL_DIR` | `/var/lib/guazza/models` | `data/models` (flag `--model-dir`) |
| `OUTPUT_DIR` | `/var/lib/guazza/output` | `data/output` (flag `--output-dir`) |
| `HEALTHCHECKS_URL` | URL Healthchecks.io | non impostata → ping saltato |
| `CONFIG_DIR` | `{repo}/config` | auto-rilevato da `__file__` |

## Regole di routing modelli — ottimizzazione costi

- **Batchare tool calls** in ogni turno quando possibile (max 32 chiamate parallele)
- **Mai loop di ricerca** su API o scraping senza limite; 3 tentativi max con backoff
- **Preferire `read`/`grep`** a `task` per ricerche in <5 file
- **Non riscrivere file interi** se `edit` può modificare il blocco rilevante
- **Commit atomici** a ogni milestone stabile per evitare perdita lavoro
- **Evitare risposte lunghe** — codice nei file, non nel testo della chat
