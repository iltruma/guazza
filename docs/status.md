# Guazza — Stato corrente

> Aggiornato: 2026-08-07 (v0.16.0)
> Storia dettagliata → `CHANGELOG.md` · Decisioni → `docs/decisions.md` · Workaround attivi → `docs/known_issues.md`
> Questo file è un cockpit: stato, coda, prossima mossa. Niente session-log (→ CHANGELOG).

## Stato

| | |
|---|---|
| Versione | **0.16.0** |
| Test | 349 verdi (suite completa ~54s) |
| Lint / mypy | ✅ puliti |
| Deploy | k3s astra/nebula, namespace `guazza`, Flux + SOPS — DB prod da resettare (schema `cape_jkg`) |

## Architettura

| Componente | Stato |
|---|---|
| Pipeline 6h | `guazza-forecast` — forecasts → features → predict+DLE+JSON |
| Ingest | `guazza-ingest historical/realtime` (SIR + Open-Meteo + Netatmo); ingestion daily in `guazza-review` |
| Modello | LightGBM quantile + CQR (cal_days=90) + ACI + rain_clf (hurdle stadio 1) |
| NWP | 4 modelli: ECMWF IFS, ICON-EU, AROME France, ICON-2I |
| Location | 6: casa_campi, lavoro_cosimo, lavoro_madda, casa_cesto, casa_nicco, casa_cercina |
| Frontend | `index.html` + `affidabilita.html` — CSS custom, Chart.js, Leaflet, RainViewer |
| Schema DB | `schema.sql` unico source of truth (13 tabelle + vista `obs_weighted_daily`) |

## Risultati CV (fold 3-4, unici rappresentativi del sistema in prod)

| Target | Skill MAE vs NWP | Note |
|---|---|---|
| tmax_c | +30-32% ✅ | Robusto, model-agnostic — obiettivo raggiunto |
| tmin_c | +7-25% ✅ | Positivo ma variabile per location (casa_nicco bias~0) |
| precip_mm | +3-11% ≈0 | Ceiling strutturale (ground truth rumoroso, D-014) |
| rain_clf | BSS +0.16/+0.28, AUC 0.73-0.79 ✅ | P(pioggia) funziona |

Coverage CI80: 72-76% (target 80%) — ACI corregge in produzione dopo ~30gg warm-up.
Fold 1-2 (pre-ott 2024) sono lead=0-only, non rappresentativi del sistema con multilead.

## Coda

### Auto-risoluzione
- **P2** — ACI in cold start (pass-through su CQR statico per le prime 30 osservazioni per bucket). Auto-risolto dopo ~30gg di operatività.

### Da implementare
- **P9** — Scheduling `guazza-hourly-correct train` (settimanale, dentro `guazza-review` o CronJob) quando lo storico realtime in prod ≥ 60gg/location. Codice pronto (D-024); oggi nessun DB ha dati sufficienti.
- **P5** — Backtest multi-anno multi-stagione. Si accumula forward dal deploy (Sprint 8+).
- **P7** — `uv_index` come dato (NWP). Nessuna nuova dipendenza; ingestion + schema + features + JSON `hourly[]`/`current`.
- **P8** — Heat index (Steadman/Rothfusz) + ondata di calore come indicatore DLE. Solo dati già presenti (T+RH realtime).
- **D-021** — Nowcast temporale 30-60min via Blitzortung. Decisione presa, da implementare.
- **D-022** — Allerte meteo Protezione Civile via allertameteo.app. Decisione presa, da implementare.

### Audit complessità 2026-08-07 (@oracle)
Conclusione: il codice è contenuto (8.1k LOC); il peso è la superficie prodotto+processo. Regime raccomandato: scienza chiusa, deploy, osservare 30-60gg in prod, feature di coda solo a basso fan-out. Azioni eseguite in sessione: status.md → cockpit, README collassato, gitignore dati locali, deps morte rimosse (polars/boto3). Dettaglio in git history.

## Prossima mossa

Deploy prod: reset DB schema (cape_jkg), `guazza-ingest historical` su k8s, `guazza-review run --force-train`, `guazza-forecast run`.
`skill_history.json` si popolerà automaticamente dopo ~1 settimana di operatività in prod.
