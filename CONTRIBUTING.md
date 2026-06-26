# Contribuire a Guazza

Repo sviluppato da Cosimo con Claude Code come agente di sviluppo.

---

## Setup

```bash
git clone <repo-url> && cd guazza
uv sync
```

## Leggere prima di lavorare — OBBLIGATORIO

**Nessuna azione prima di aver letto questi file:**

1. `docs/status.md` — stato corrente, cosa è fatto, prossimi passi (`🟡` = punto aperto)
2. `AGENTS.md` — regole di progetto, stack, guardrail (fonte unica; `CLAUDE.md` è symlink per retrocompatibilità)
3. `docs/decisions.md` — perché le cose sono come sono
4. `docs/known_issues.md` — workaround attivi

---

## Flusso di lavoro

### Un task alla volta

Non iniziare il passo N+1 se N non è completato e testato.

### Prima di ogni modifica

Verificare che i test attuali passino:

```bash
uv run pytest -x -q
uv run ruff check src/ tests/
uv run mypy src/
```

Se non passano → fermarsi, non aggiungere complessità sopra un problema aperto.

### Formato commit

```
<tipo>(<scope>): <descrizione breve in italiano>
```

Tipi: `feat`, `fix`, `test`, `docs`, `config`, `refactor`, `chore`

Esempi:
```
feat(fetchers): aggiungi fetch Open-Meteo multi-modello
fix(storage): correzione upsert duplicati in observations
docs(decisions): documenta D-011 scelta embargo CV
```

### Staging selettivo — OBBLIGATORIO

```bash
# Corretto
git add src/guazza/fetchers.py tests/test_fetchers.py

# Vietato
git add -A
git add .
```

### Push: vietato

`git push` non va mai eseguito dagli agenti. L'utente gestisce il push.

---

## Checklist completamento task

- [ ] Codice tipato, mypy passa
- [ ] Test pytest: happy path + almeno un edge case
- [ ] `ruff check` zero warning
- [ ] Log strutturato dove rilevante
- [ ] Healthchecks.io ping se è un job cron
- [ ] `docs/known_issues.md` aggiornato se workaround
- [ ] Commit creato con formato corretto

---

## Guardrail zona rossa — STOP e chiedi conferma

| Categoria | Esempi |
|---|---|
| Rete reale | `httpx.get(...)`, curl, wget |
| Scrittura DB | `INSERT`, `UPDATE`, `DELETE`, `ALTER TABLE` su DuckDB |
| File dati | scrittura su `/var/lib/guazza/`, Parquet esistenti |
| Dipendenze | `uv add`, `pip install` non in pyproject.toml |

Pattern obbligatorio:
```
Sto per eseguire:
  [tipo]: [dettaglio — URL / query SQL / comando]
  Scopo: [perché]
  Impatto: [cosa cambia]

Attendo conferma.
```

---

## Errori in esecuzione

1. Fermarsi immediatamente
2. Mostrare l'output completo
3. Aspettare istruzioni — no fix autonomi, no varianti

---

## Aggiornamento documentazione

- `docs/status.md` → aggiornare a fine sessione
- `docs/known_issues.md` → aggiornare se si trova un workaround non ovvio
- `docs/decisions.md` → aggiornare per nuove decisioni architetturali (`D-NNN`)
