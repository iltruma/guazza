# Documentation Index

`AGENTS.md` (root) è la fonte unica delle istruzioni di progetto. Il repo è gestito con agenti AI (Claude, Gemini, DeepSeek).

## Sempre a inizio sessione

- `AGENTS.md` — istruzioni di progetto (fonte unica; `CLAUDE.md` è symlink)
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
