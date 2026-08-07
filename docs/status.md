# Guazza — Stato corrente

> Aggiornato: 2026-08-07 (v0.15.0)
> Storico sprint → `CHANGELOG.md` · Decisioni → `docs/decisions.md` · Workaround attivi → `docs/known_issues.md`

## Stato

| | |
|---|---|
| Versione | **0.16.0** |
| Test | 315 verdi (suite completa ~41s) |
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

## Prossima mossa

Deploy prod: reset DB schema (cape_jkg), `guazza-ingest historical` su k8s, `guazza-review run --force-train`, `guazza-forecast run`.
`skill_history.json` si popolerà automaticamente dopo ~1 settimana di operatività in prod.

---

## Sessione 2026-08-07 — correttore orario + QC realtime esteso

- **Correttore orario** (commit 2e014fd, D-024): nuovo modulo `hourly_corrector.py` + CLI `guazza-hourly-correct` (train/eval/status). LightGBM p50 sul residuo di shape Δ(h) = obs_mediana − shape NWP normalizzata; embargo 7gg, split cronologico, salvataggio solo con improvement RMSE ≥ 15% su holdout. `compute_hourly_profile` accetta il correttore: delta + ri-ancoraggio a [tmin_p50, tmax_p50] e bande CI80 ai rispettivi bound — livelli sempre ML daily, cambia solo la forma; fallback automatico se il file manca.
- **QC realtime** (`qc.py`): 3 flag nuovi nel batch idempotente — `spike_realtime` (Δ>8°C entro 90min), `stall_sensor` (costante ≥180min), `bias_solar` (Netatmo 10-17 locali + cielo sereno NWP modale) — consumati dal dataset del correttore.
- **Dati**: nessun DB ha ancora storico realtime sufficiente (prod da resettare); il training si attiva da solo quando l'accumulo raggiunge ~60gg/location (P9).
- **Review @oracle + fix** (commit 2e014fd, D-024): revisione architetturale → 1 bug bloccante trovato (bande CI80 che si incrociavano con correttore attivo, ri-ancoraggio indipendente p50/bande) e fixato: bande derivate dalla posizione normalizzata del p50 corretto → `lo ≤ p50 ≤ hi` garantita per costruzione con bound asimmetrici CQR+ACI (P1, prima dell'attivazione in prod). `eval_X/eval_y` → `eval_set` canonico (P3). 8 test nuovi.
- **P(pioggia) oraria** (`precip_prob_ml` in `hourly[]`): prob daily ML (`rain_clf.prob_rain`) distribuita sul timing NWP (`precip_prob` normalizzata a max=1). Semantica esplicita: P che l'ora h sia l'ora di pioggia dato giorno piovoso — non è prob oraria calibrata. Tooltip frontend "P pioggia ML" (vista Guazza daily/weekly). Nessun nuovo modello/target (opzione 1 oracle; classificatore orario rifiutato: D-014/D-005).
- **Follow-up da review oracle** (non urgenti): P4 — `bias_solar` carica i weather_code senza bound temporale (aggiungere `WHERE ts_valid >=`); P5 — `stall_sensor` flagga solo la coda della run (i primi ~180min di stallo restano nel training del correttore; decidere se flaggare l'intera run); `docs/contract.md` da aggiornare (descrizione bande con correttore + campo `precip_prob_ml`) — rimandato perché il file ha modifiche non committate dell'utente.
- Nota: 2 test riferivano `netatmo_fetch_log` (tabella rimossa in 54ed68f) — sistemati in sessione (test della feature rimossa eliminato, idempotenza su observations preservata).

## Sessione 2026-08-06 — daily fuori dal cron, review con finestra di recupero

- `guazza-ingest daily` rimosso dallo scheduling: l'ingestion giornaliera era duplicata 1:1 in `guazza-review` (stessa data, stesso orario). Resta come strumento manuale (recupero giorni mancanti con `--date`, `--netatmo-all`).
- `guazza-review` ingesta la finestra [ieri-7, ieri] (SIR CSV + OM historical + multilead): costo rete invariato (il CSV SIR restituisce comunque tutto lo storico), auto-guarigione dai run persi entro una settimana. Netatmo daily resta su ieri.
- **Skill curve da predictions reali**: `_run_skill_curve` non usa più il modello congelato su split fisso (2025-10-15) — curva per-lead da predictions di produzione (p50, dedup ultima model_version) + consensus NWP da features_daily, finestra mobile 90gg + embargo 7gg. Ground truth `obs_weighted_daily`. Payload invariato, caption frontend aggiornata. Da notare: `skill.json` ora misura il sistema reale; le prediction di produzione esistono dal deploy (ott 2025), quindi la curva parte da lì.
- **Copertura CI + affidabilità onesta**: payload `skill.json` esteso con `coverage` per location (CI80/CI90 empirici per lead D+0..D+7, intervalli CQR+ACI di produzione); nuova card "Copertura intervalli" in affidabilita.html; caption "chi vince" riformulate (vittoria = errore minore quel giorno, anche di poco) + MAE medio per modello in legenda. Allineato al protocollo scientifico concordato: la pagina mostra dove si vince e dove no, niente test statistici (sono per il paper/P5).
- **P(pioggia)**: `rain_prob` persistita in predictions (colonna nuova, si popola forward dal deploy) → sezione `rain_prob` in skill.json con Brier per lead (Guazza vs NWP-consensus binario, stessa baseline di cv.py) + prob media giorni piovosi/asciutti; nuova card "P(pioggia)" in affidabilita e pill `NN%` nelle card giornaliere di index e nel dettaglio espanso (probabilità del totale giornaliero, non per-ora — l'orario ha già `precip_prob` NWP per i colori delle barre). Il risultato più forte del modello (BSS +0.16/+0.28 in CV) diventa visibile in pagina.
- Da fare nell'infra repo (astra): rimuovere il CronJob `guazza-ingest daily`.

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
