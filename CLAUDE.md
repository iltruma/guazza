@AGENTS.md

## Note OpenCode / Claude

- **STOP a inizio sessione: leggi `docs/status.md` integralmente prima di qualsiasi azione**
- Una domanda alla volta se serve chiarimento
- Rispondi sempre in italiano

### Spiegazioni orientate alla manutenibilità

Quando spieghi una modifica (vedi "Spiegazione obbligatoria prima di modificare file" in AGENTS.md),
calibra la spiegazione così:
- **Perché**: il problema tecnico concreto, non la descrizione astratta
- **Come**: le scelte implementative rilevanti — cosa hai considerato, cosa hai scartato e perché
- **Impatto**: cosa cambia per chi legge o modifica il codice dopo

L'obiettivo non è validare la scelta, ma permettere all'utente di capire, valutare e mantenere il codice in autonomia.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
