# Guazza — Stato corrente

> Aggiornato: 2026-08-06 (v0.15.0)
> Storico sprint → `CHANGELOG.md` · Decisioni → `docs/decisions.md` · Workaround attivi → `docs/known_issues.md`

## Stato

| | |
|---|---|
| Versione | **0.15.0** |
| Test | 315 verdi (suite completa ~41s) |
| Lint / mypy | ✅ puliti |
| Deploy | k3s astra/nebula, namespace `guazza`, Flux + SOPS — DB prod da resettare (schema `cape_jkg`) |

## Architettura

| Componente | Stato |
|---|---|
| Pipeline 6h | `guazza-forecast` — forecasts → features → predict+DLE+JSON |
| Ingest | `guazza-ingest historical/daily/realtime` (SIR + Open-Meteo + Netatmo) |
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
- **P5** — Backtest multi-anno multi-stagione. Si accumula forward dal deploy (Sprint 8+).
- **P7** — `uv_index` come dato (NWP). Nessuna nuova dipendenza; ingestion + schema + features + JSON `hourly[]`/`current`.
- **P8** — Heat index (Steadman/Rothfusz) + ondata di calore come indicatore DLE. Solo dati già presenti (T+RH realtime).
- **D-021** — Nowcast temporale 30-60min via Blitzortung. Decisione presa, da implementare.
- **D-022** — Allerte meteo Protezione Civile via allertameteo.app. Decisione presa, da implementare.

## Prossima mossa

Deploy prod: reset DB schema (cape_jkg), `guazza-ingest historical` su k8s, `guazza-review run --force-train`, `guazza-forecast run`.
`skill_history.json` si popolerà automaticamente dopo ~1 settimana di operatività in prod.

---

## Sessione 2026-08-04 — CV, cleanup ML, LEAD_BUCKETS giornalieri

### Risultati empirici

- **walk_forward_cv** girata su DB locale riempito con `ingest historical`
- Identificato che fold 1-2 sono lead=0-only (archivio Open-Meteo `previous_dayN` parte da mar 2024 per ECMWF, gen 2024 AROME, apr 2025 ICON-2I)
- Archivio multilead frammentato per anno/modello (solo ICON-EU completo nel 2024, ECMWF domina lead lunghi, ICON-2I solo ≤48h)
- Consensus Oracle: tmax +30% è reale e robusto; precip intensità è ceiling strutturale; capitolo ML chiuso tranne calibrazione CQR

### Commit

- **2dbc4b9** — `LEAD_BUCKETS` ridefiniti giornalieri (D+0..D+5+) — i vecchi bucket orari erano sempre vuoti su features_daily multi-lead
- **fba2bd0/9c0a01d** — `cal_days` 150 (era 90) poi rollbackato a 90 — coverage CI non migliorava (non-stazionarietà train→test, non volume cal set)
- **5083290** — Rimossi wet regressor (skill negativo in 3/4 fold) e `anomaly_targets` (dead code post-KI-024); 36 test rimasti verdi

### Note tecniche

- La coverage CI80 (72-76% vs 80%) è non-stazionarietà train→test, non problema di volume cal set. ACI è il meccanismo corretto per produzione.
- `walk_forward_cv` e `crps_from_quantiles` sono in `cv.py` (estratti da `models.py` in v0.15.0) — strumenti del case study, verranno riusati tra 6 mesi quando ensemble ≤48h si completa.
- Wet regressor rimosso: skill_wet_mae negativo perché usa stesse FEATURE_COLS del modello globale con meno dati — nessun vantaggio strutturale senza feature termodinamiche aggiuntive specifiche.
- `anomaly_targets` rimosso: ANOMALY_TARGETS=() da KI-024, tutti i branch erano dead code. Semplificata `_target_col`, rimossa `_invert_anomaly`.

### Sessione 2026-08-03 — CAPE + hurdle model

- CAPE feature convettiva aggiunta (commit 9c289f9): `cape_jkg` in schema/storage/features, `nwp_cape_mean/spread` in FEATURE_COLS (ora 32, 4×8)
- rain_clf (hurdle stadio 1): BSS +0.16/+0.28, AUC 0.73-0.79 nei fold rappresentativi ✅
- Wet regressor (hurdle stadio 2): rimosso in sessione 2026-08-04 (skill negativo)

### Sessione 2026-08-04 — chunk size ingestion Open-Meteo

- Budget-celle `_OM_CELL_BUDGET = 483_840`: chunk ottimale per modello via `_chunk_days(n_vars)`
- Chunk risultanti: historical 474gg, ECMWF multilead 120gg, ICON-EU 211gg, AROME 845gg, ICON-2I 423gg
