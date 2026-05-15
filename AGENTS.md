# AGENTS.md — Regole per Claude Code

## ⛔ REGOLA PRINCIPALE — LEGGI PRIMA DI TUTTO

Claude Code **NON esegue operazioni esterne autonomamente**, mai.

Per qualsiasi operazione che tocca il mondo reale, il flusso obbligatorio è:

1. **Mostra** cosa stai per fare (codice o comando esatto)
2. **Spiega** brevemente perché
3. **Scrivi** esplicitamente: `"Attendo conferma prima di procedere."`
4. **Esegui** solo dopo risposta esplicita dell'utente (`"procedi"`, `"ok"`, `"vai"`)

Questo vale **sempre**, anche se sembra innocuo, anche se l'hai già fatto prima,
anche se l'utente ha detto "fai tutto" in modo generico.

### Operazioni che richiedono autorizzazione esplicita

```
🔴 STOP — mostra e aspetta conferma:

  Rete:        qualsiasi chiamata HTTP reale (API, scraping, download)
  Database:    scritture DuckDB (INSERT, UPDATE, DELETE, ALTER, migrazioni)
  File dati:   scrittura su /var/lib/guazza/, config/*.yaml, Parquet esistenti
  Dipendenze:  installazione pacchetti non in pyproject.toml
```

### Operazioni che può fare autonomamente

```
🟢 PROCEDI senza chiedere:

  - Scrivere/modificare codice in src/, tests/, scripts/
  - Leggere qualsiasi file
  - Eseguire pytest, ruff, mypy
  - Scrivere/aggiornare docs/
  - git add <file specifici> + git commit  (mai git push)
```

### Pattern obbligatorio prima di ogni operazione in zona rossa

```
Sto per eseguire:
  [tipo]: [dettaglio esatto — URL, query SQL, comando]
  Scopo: [perché è necessario]
  Impatto: [cosa cambia/scrive/modifica]

Attendo conferma prima di procedere.
```

---

## Identità del progetto

Questo è **Guazza**: sistema ML di post-processing meteo iper-locale per microclimi toscani. Progetto personale con ambizioni scientifiche, sviluppato in spare time su orizzonte 12-18 mesi.

## Principi generali

**Sii diretto.** L'utente è un Cloud Architect con background ML. Non spiegare cose ovvie, non ripetere la domanda, non mettere summary finali.

**Una domanda alla volta.** Se hai bisogno di chiarimenti, fai la domanda più importante. Non un elenco di cinque.

**Preferisci boring technology.** Cron > Prefect. DuckDB > PostgreSQL. Script Python > framework complesso. Se funziona e costa meno da mantenere, è la scelta giusta.

**Codice tipato.** Tutto il codice ha type hints. mypy deve passare. pydantic v2 per i modelli dati in input/output.

**Test obbligatori.** Ogni modulo non banale ha test pytest prima di essere considerato completato. Almeno happy path + edge case principale.

## Anti-pattern — mai proporre queste cose

```
❌ Coolify, Portainer, o qualsiasi PaaS layer
❌ Prefect, Dagster, Airflow, Celery come orchestratori
❌ Kubernetes, Docker Swarm, ArgoCD
❌ PostgreSQL, MySQL, Redis, MongoDB
❌ GitHub Actions come orchestratore runtime di job
❌ ERA5 come predittore di forecast (solo come climatologia statica)
❌ Embargo < 7 giorni nella cross-validation
❌ Valori puntuali nudi senza confidence interval
❌ Deep learning come modello core (confronto benchmark OK, core no)
❌ Raspberry Pi in produzione (rimosso dal progetto)
❌ Streamlit o Gradio per il frontend
❌ FastAPI come processo 24/7 per single user
```

Se uno di questi appare come dipendenza necessaria per risolvere un bug, segnalalo all'utente e proponi un'alternativa conforme allo stack.

## Esecuzione script — guardrail obbligatorio

Quando esegui uno script o comando e va in errore:

1. **Fermati immediatamente** — non tentare fix autonomi, non ritentare varianti
2. **Mostra l'errore** esattamente com'è (output completo, non riassunto)
3. **Aspetta istruzioni** — l'utente decide se è un bug del codice, un problema di config, o un errore atteso

Quando uno script che NON hai ancora eseguito sta per fare operazioni in zona rossa
(API, DB, file dati), applica il pattern "Attendo conferma" **prima** di eseguirlo,
non solo se va in errore.

Non fare reverse engineering autonomo su API o sistemi esterni quando lo script fallisce. Chiedi.

## Come gestire i punti aperti

Il file `docs/status.md` elenca i punti aperti correnti con tag `🟡`. Quando ti imbatti in uno di questi durante il lavoro:

1. **Fermati** — non inventare un valore di default silenzioso
2. **Segnala** — "Questo task richiede una decisione sul punto aperto #3 (soglie indicatori). Default proposto: X. Procedo con X o vuoi specificare?"
3. **Aspetta conferma** prima di procedere se il punto è bloccante

Se il punto aperto non è bloccante per il task corrente, continua e nota in fondo alla risposta: "Nota: questo modulo usa il default per il punto aperto #3, da aggiornare quando definito."

## Gestione errori e problemi noti

Quando trovi un problema tecnico che richiede un workaround non ovvio:

1. Aggiungi una entry a `docs/known_issues.md`
2. Documenta il problema, il workaround, e il link alla issue upstream se esiste
3. Metti un commento `# KNOWN ISSUE: vedi docs/known_issues.md#N` nel codice

## Scraper fragili — best practice

CFR Toscana e ARPAT hanno HTML instabile. Per ogni scraper:

- Wrap tutto in `try/except` con `tenacity` exponential backoff (3 tentativi, delay 60s, 300s, 600s)
- Log strutturato con `loguru`: ogni run → `{"scraper": "cfr_realtime", "status": "ok|fail", "ts": ..., "rows": N}`
- Ping Healthchecks.io alla fine di ogni run riuscito
- Se il run fallisce dopo tutti i retry: log `ERROR`, ping Healthchecks.io con fail, **non crashare** il processo — il prossimo cron riprova

## ERA5 — regola critica

ERA5 è una reanalisi: assimila osservazioni reali. Usarlo come predittore nel modello di forecast introduce train/serve skew perché in produzione usi Open-Meteo forecasts (che non hanno visto la verità). **Questo è un errore metodologico documentato.**

ERA5 può essere usato solo per:
- Features climatologiche statiche (media/std mensile multi-decennale)
- Ground truth alternativo per location senza stazioni SIR
- Backfill storico solo come target (osservazione), mai come predittore

Se vedi ERA5 usato come input dinamico a un modello: **è un bug**, non una feature.

## Decision Logic Engine — logging obbligatorio

Ogni invocazione del DLE deve produrre un log entry in DuckDB (`indicator_log`):

```python
{
    "ts": datetime,
    "location_id": str,
    "indicator_id": str,
    "input_summary": dict,   # distribuzione input condensata
    "rule_matched": str,     # quale regola ha scattato
    "verdict": str,          # verde/giallo/rosso
    "probability": float
}
```

Questo è non negoziabile: senza log non possiamo fare post-mortem per l'articolo e non possiamo calibrare le soglie.

## Output JSON — contract obbligatorio

Il JSON di output per il frontend deve sempre includere:

```json
{
  "coverage_empirical_30d": {
    "temp_ci80": float,   // copertura empirica CI nominale 80%
    "temp_ci90": float
  }
}
```

Questo campo va calcolato sul rolling window degli ultimi 30 giorni di coppie (prediction, observation). Se non ci sono ancora 30 giorni di dati, il campo è `null` e la dashboard mostra "calibrazione in corso".

## Progressione del lavoro

Il progetto ha sprint non contigui (spare time). Ogni sprint:

1. Leggi `docs/status.md` per capire dove siamo
2. Completa il task corrente con test
3. Suggerisci aggiornamento a `docs/status.md` a fine sessione
4. Se hai trovato problemi: aggiorna `docs/known_issues.md`

Non assumere mai che l'ambiente locale sia "pulito" all'inizio di una sessione. Verifica sempre che i test passino prima di procedere con nuove feature.

## Git — regole di commit

### ✅ Commit: autonomo, obbligatorio al completamento di ogni task

Al completamento di ogni task (step, modulo, script), Claude Code **deve** creare un commit senza aspettare conferma. Il commit è parte della checklist di completamento.

**Trigger obbligatori per un commit:**
- Task completato con test verdi e lint pulito
- Aggiornamento di `docs/status.md` o `docs/known_issues.md`
- Aggiunta di configurazione (`config/*.yaml`) nuova o modificata
- Qualsiasi milestone intermedia stabile (es. schema DuckDB definito, primo fetcher funzionante)

**Regole di staging:**
- Aggiungi solo i file pertinenti al task — mai `git add -A` o `git add .` ciecamente
- Non committare mai: `.env`, file con credenziali, dati grezzi (`*.parquet`, `*.db`), output temporanei

**Formato del messaggio di commit:**
```
<tipo>(<scope>): <descrizione breve in italiano>

[corpo opzionale se serve contesto]
```
Tipi: `feat`, `fix`, `test`, `docs`, `config`, `refactor`, `chore`
Scope: il modulo o componente (es. `ingestion`, `storage`, `indicators`, `config`)

Esempi validi:
```
feat(storage): schema DuckDB iniziale con tabelle forecasts e observations
test(ingestion): happy path + edge case fetcher Open-Meteo
docs(status): aggiorna stato Sprint 1 dopo completamento schema
config(locations): aggiunge coordinate complete per le 4 location
```

### ⛔ Push: mai, in nessun caso

`git push` è **vietato** incondizionatamente. Nemmeno se l'utente dice "fai tutto" o "push pure". L'utente gestisce il push manualmente.

Se per qualsiasi motivo Claude Code si trova a voler fare push, si ferma e segnala: "Il push è escluso dalle operazioni autonome — effettualo tu manualmente."

### Note operative

- Se `pre-commit` o hook fallisce, **non usare `--no-verify`** — fermarsi, mostrare l'errore, aspettare istruzioni
- Preferire commit atomici (un task = un commit) rispetto a mega-commit multi-step
- Se il task è in più sotto-step, un commit per sotto-step è preferibile a uno solo finale

---

## Checklist per task completato

Prima di dichiarare un task completato:

- [ ] Codice tipato, mypy passa
- [ ] Test pytest scritti e verdi
- [ ] `ruff check` passa (zero warning)
- [ ] Log strutturato presente dove rilevante
- [ ] Healthchecks.io ping presente se è un job cron
- [ ] Punto aperto segnalato se il task ne dipende
- [ ] `docs/known_issues.md` aggiornato se ci sono workaround
- [ ] **Commit creato** con messaggio conforme al formato `tipo(scope): descrizione`
