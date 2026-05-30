# Documentation Index

Repo Claude-only. `CLAUDE.md` (root) è la fonte unica delle istruzioni di progetto.

## Sempre a inizio sessione

- `CLAUDE.md` — istruzioni di progetto (fonte unica)
- `docs/status.md` — stato corrente, unica fonte di verità (punti aperti `🟡`)

## Riferimento on-demand (caricare quando serve)

- `docs/contract.md` — contract JSON di output + logging DLE
- `docs/frontend.md` — librerie client-side CDN (design completo in `DESIGN.md`)
- `docs/decisions.md` — decisioni scientifiche in dettaglio
- `docs/known_issues.md` — workaround non ovvi
- `README.md` — albero directory + comandi operativi con tutti i flag
- `DESIGN.md` — design system frontend

## Note

- `docs/archive/` e `docs/learnings/` — non auto-caricare.
- I file in `.claude/` (hook, sessions, completions del token-optimizer) sono esclusi dal
  `.claudeignore` e dal `.gitignore`: tooling locale, non documentazione canonica.
