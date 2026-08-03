# Guazza — Stato corrente

> Aggiornato: 2026-08-03 (v0.13.0 → v0.14.0-dev)
> Storico sprint → `CHANGELOG.md` · Decisioni → `docs/decisions.md` · Workaround attivi → `docs/known_issues.md`

## Stato

| | |
|---|---|
| Versione | **0.13.0** |
| Test | 316 verdi (suite completa; `test_models.py` ~3min per LightGBM training) |
| Lint / mypy | ✅ puliti |
| Deploy | k3s astra/nebula, namespace `guazza`, Flux + SOPS |

## Architettura

| Componente | Stato |
|---|---|
| Pipeline 6h | `guazza-pipeline` — forecasts → features → predict+DLE+JSON → skill-history → monitor |
| Ingest | `guazza-ingest historical/daily/realtime` (SIR + Open-Meteo + Netatmo) |
| Modello | LightGBM quantile + CQR + ACI (AdaptiveConformalizer) |
| NWP | 4 modelli: ECMWF IFS, ICON-EU, AROME France, ICON-2I |
| Location | 6: casa_campi, lavoro_cosimo, lavoro_madda, casa_cesto, casa_nicco, casa_cercina |
| Frontend | `index.html` + `affidabilita.html` — CSS custom, Chart.js, Leaflet, RainViewer |
| Schema DB | `schema.sql` unico source of truth (13 tabelle + vista `obs_weighted_daily`) |

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

Calibrazione soglie DLE post-30gg `indicator_log` in produzione.

---

## Sessione 2026-08-03 — Miglioramento training ML

### Fix chirurgici (commit c5f0af6)
- **Quantile crossing**: sort anti-crossing (`_Q_ORDER`) in `predict`/`predict_frame`
- **ACI γ allineato a D-019**: `ACI_LEARNING_RATE = 0.005` (era 0.02 hardcoded — 4× troppo aggressivo per drift stagionale); costante unica condivisa tra `models.py` e `pipeline.py`
- **Fallback CQR bucket adiacente** (commit c5f0af6): bucket con <10 campioni ora usa il bucket adiacente più vicino con dati sufficienti, non il pool globale (il vecchio fallback produceva CI troppo stretti sui lead lunghi — contribuiva a KI-023)

### Feature engineering (commit da lanciare in CV per misurare Δ)
Tre commit incrementali separati per misurare l'effetto di ogni cambiamento:

1. **Commit 1 — feature engineering puro** (`features.py` + `FEATURE_COLS`):
   - Pressione superficiale NWP (`pressure_hpa_avg`, `pressure_hpa_min`) per modello + ensemble mean/spread — già in `forecasts`, non usata. Proxy del regime sinottico (alta pressione → inversione notturna → Tmin bassa)
   - Lag-2 obs + gradient termico (`obs_tmin_d2`, `obs_tmax_d2`, `obs_tmin_gradient`, `obs_tmax_gradient`) — inerzia termica del microclima
   - Cicliche doy (`doy_sin`, `doy_cos`) — elimina la discontinuità gen/dic per LightGBM
   - `NWP_FEATURE_COLS` ora 28 colonne (4 modelli × 7 variabili, era 20)

2. **Commit 2 — early stopping embargato** (`models.py`):
   - `n_estimators` 500 → 2000 (tetto)
   - `_train_lgbm`: early stopping su validation set embargato opzionale (`early_stopping(50)`)
   - `_es_val_split`: helper condiviso per split `[fit][gap 7d][es_val 30d][gap 7d][cal/test]`
   - `train_all` e `walk_forward_cv` usano `_es_val_split`

3. **Commit 3 — init_score = nwp_mean** (`models.py`):
   - Il modello impara il residuo NWP invece del livello assoluto
   - `_predict_level`: helper unico raw→livello assoluto usato in `_compute_cqr`, `predict`, `predict_frame`, `walk_forward_cv` (evita che la logica sia replicata in 4 punti con rischio di incoerenza)
   - `_make_init_score`: calcola `nwp_mean` con fallback `clim → 0` (gestisce i NULL)
   - `TrainingArtifacts.init_score_targets` persistito nel manifest JSON

### 🟡 Da fare: lanciare walk_forward_cv con il nuovo codice
Richiede riesecuzione su DB produzione (feature_daily rebuild + CV). Misurare Δ MAE/skill per tmin/tmax/precip rispetto alla baseline (tmin +15.6%, tmax +42.6%, precip -2.9%) e metriche classificatore (Brier, BSS, AUC) + `skill_wet_mae` del wet regressor.

### Sessione 2026-08-03 (cont.) — CAPE + hurdle model stadio 2

**Task 2 — CAPE feature convettiva (commit 9c289f9)**
- `fetch_openmeteo.py`: `cape` → `cape_jkg` in `_OM_HOURLY_VARS`/`_OM_VAR_MAP`
- `schema.sql`: colonna `cape_jkg DOUBLE` in `forecasts` (null se modello non espone)
- `storage.py`: `cape_jkg` in INSERT/UPSERT
- `features.py`: `MAX(cape_jkg) AS cape_max` in `daily_nwp`; `NWP_DAILY_VARS` += `cape_max`; `nwp_cape_mean/spread` in ensemble stats → `NWP_FEATURE_COLS` ora 32 (4×8)
- `models.py`: `FEATURE_COLS` += `nwp_cape_mean`, `nwp_cape_spread`
- Verifica empirica: tutti e 4 i modelli NWP espongono CAPE senza null (incl. icon_2i)
- **Nota deploy**: DB prod va resettato (no migrazione; `cape_jkg` non esiste nella tabella esistente)

**Task 3 — Hurdle model stadio 2 (commit 3a8a0d0)**
- `_train_wet_regressor()`: 5 quantili LightGBM su wet days (>0.2mm), CQR stratificata per lead bucket, no init_score, `ValueError` se <30 wet days
- `TrainingArtifacts.wet_regressor: ModelBundle | None` (retrocompatibile, default None)
- `train_all`: addestra wet_reg dopo rain_clf
- `_save_artifacts`/`load_artifacts`: 5 file `wet_reg_q{nn}.txt` + manifest
- `predict`/`predict_frame`: `out["rain_clf"]["wet_reg"]` con anti-crossing e clamp ≥ 0
- `walk_forward_cv`: metriche `mae_wet`/`skill_wet_mae` su test wet-only
- `docs/contract.md`: aggiunto `precip_mm.wet_reg` al JSON contract

### Prossimi candidati (non implementati)
- **Esporre `wet_reg` nel frontend** — card "se piove, quanti mm?" distinta da `prob_rain`
- **`walk_forward_cv` su DB prod** — misurare `skill_wet_mae` e decidere se il wet regressor vale
- **Target ML vento avg** (verificare disponibilità ground truth SIR per location)
- **Optuna tuning** LightGBM (dopo feature stabili: `num_leaves` 7-31, `learning_rate` 0.02-0.1)
- **cal_days 120/180** (verifica coverage per bucket — D-003 richiede ~200 campioni/bucket)
