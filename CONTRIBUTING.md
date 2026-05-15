# Contribuire a Guazza

Questo repo è sviluppato da un team misto: umano (Cosimo) + agenti AI (Claude/OpenCode,
Gemini, GPT, altri). Le regole qui sotto valgono per tutti, umani e agenti.

---

## Prerequisiti

```bash
# Clone e setup
git clone <repo-url> && cd guazza
uv sync

# Installa pre-commit hook
cp .githooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
# oppure:
git config core.hooksPath .githooks
```

## Leggere prima di lavorare

Obbligatorio a inizio sessione:

1. `docs/status.md` — stato corrente, cosa è fatto, prossimi passi
2. `AGENTS.md` — regole di progetto, stack, guardrail
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
Scope: modulo o componente (`ingestion`, `storage`, `indicators`, `config`, ecc.)

Esempi validi:
```
feat(fetchers): aggiungi fetch Open-Meteo multi-modello
fix(storage): correzione upsert duplicati in observations
test(indicators): aggiungi test edge case DLE gelata
docs(decisions): documenta D-011 scelta embargo CV
```

### Staging selettivo — OBBLIGATORIO

```bash
# Corretto: solo i file del task corrente
git add src/guazza/fetchers.py tests/test_fetchers.py

# Vietato
git add -A
git add .
```

### Push: vietato

`git push` non va mai eseguito dagli agenti. L'utente gestisce il push.

---

## Checklist prima di considerare un task completato

- [ ] Codice tipato (type hints ovunque), mypy passa
- [ ] Test pytest scritti: happy path + almeno un edge case
- [ ] `ruff check` zero warning
- [ ] Log strutturato (`loguru`) dove rilevante
- [ ] Healthchecks.io ping se è un job cron
- [ ] `docs/known_issues.md` aggiornato se workaround
- [ ] Commit creato con formato corretto

---

## Guardrail zona rossa — STOP e chiedi conferma

Queste azioni richiedono conferma esplicita prima di procedere:

| Categoria | Esempi |
|---|---|
| Rete reale | `httpx.get(...)`, curl, wget su URL esterni |
| Scrittura DB | `INSERT`, `UPDATE`, `DELETE`, `ALTER TABLE` su DuckDB |
| File dati | scrittura su `/var/lib/guazza/`, Parquet esistenti |
| Dipendenze | `uv add <pacchetto>`, `pip install` non in pyproject.toml |

Pattern obbligatorio prima di procedere:
```
Sto per eseguire:
  [tipo]: [dettaglio — URL / query SQL / comando]
  Scopo: [perché è necessario]
  Impatto: [cosa cambia/scrive/modifica]

Attendo conferma.
```

---

## Errori in esecuzione

1. Fermarsi immediatamente
2. Mostrare l'output completo dell'errore
3. Aspettare istruzioni — non tentare fix autonomi, non ritentare varianti

---

## Stack e decisioni architetturali

Lo stack è blindato. Prima di proporre un'alternativa, verificare in `AGENTS.md`
sezione "Anti-pattern" e in `docs/decisions.md`. Se un bug tecnico reale impone
una deviazione, documentarla in `docs/decisions.md` con motivazione.

---

## Aggiornamento documentazione

- `docs/status.md` va aggiornato a fine sessione con: cosa è stato fatto,
  prossimi passi, punti aperti (tag `🟡`)
- `docs/known_issues.md` va aggiornato se si trova un workaround non ovvio
- `docs/decisions.md` va aggiornato se si prende una nuova decisione
  architetturale (formato `D-NNN`)
