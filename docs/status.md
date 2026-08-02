# Guazza — Stato corrente

> Aggiornato: 2026-08-02 (v0.13.0)
> Storico sprint → `CHANGELOG.md`

## Stato

| | |
|---|---|
| Versione | **0.13.0** |
| Test | 337 verdi (suite completa; `test_models.py` ~3min per LightGBM training) |
| Lint / mypy | ✅ puliti |
| Deploy | **Sprint 8 completato** (v0.12.2) — k3s astra/nebula, namespace `guazza`, Flux + SOPS |

## Architettura corrente

| Componente | Stato |
|---|---|
| Pipeline 6h | `guazza-pipeline` — forecasts → features → predict+DLE+JSON → skill-history → monitor |
| Ingest | `guazza-ingest historical/daily/realtime` (SIR + Open-Meteo + Netatmo) |
| Modello | LightGBM quantile + CQR + ACI (AdaptiveConformalizer, Gibbs & Candès 2021) |
| NWP | 4 modelli: ECMWF IFS, ICON-EU, AROME France, ICON-2I |
| Location | 6: casa_campi, lavoro_cosimo, lavoro_madda, casa_cesto, casa_nicco, casa_cercina |
| Frontend | `index.html` + `affidabilita.html` — CSS custom, Chart.js, Leaflet, RainViewer |
| Schema DB | `schema.sql` unico source of truth (13 tabelle + vista `obs_weighted_daily`) |

## Punti aperti 🟡

### P2 — ACI in cold start

`AdaptiveConformalizer` entra in warm mode dopo 30 aggiornamenti per (target, lead_bucket).
Fino ad allora `apply_aci_correction` è un pass-through su CQR statico.
Si risolve automaticamente dopo ~30 giorni di operatività. Nessuna azione richiesta.

### P4 — `affidabilita.html` come pagina dedicata

Oggi la sezione "Quanto è affidabile" è embeddata nello SPA per-location.
`skill.json` è già globale — candidato a pagina statica separata in Sprint 11 (case study).

### P5 — Backtest multi-anno gated su accumulo forward

`previous_dayN` Open-Meteo parte da ~nov 2025. La versione rigorosa multi-stagione
si accumula solo in avanti dall'avvio in produzione (Sprint 8+).

### P7 — UV index come dato (NWP)

Aggiungere `uv_index` (e opzionalmente `uv_index_clear_sky`) per ogni ora/modello NWP. Campo
già standard nell'Open-Meteo Forecast API → ingestion + schema + features senza nuove dipendenze.
Esporre in JSON `hourly[]` e `current` (valore realtime dal modello a lead breve).
Non è un indicatore DLE di per sé, ma abilita derivati futuri (scottatura, esposizione prolungata).

### P8 — Heat index + ondata di calore come indicatore DLE (stile "panni")

- **Heat index istantaneo**: T + RH (SIR/Netatmo realtime, abbondanti) → Steadman o Rothfusz
- **Ondata di calore**: ≥3gg consecutivi con Tmax > 35°C (soglia Protezione Civile) oppure
  heat index notturno > 23°C (notti tropicali)
- Nuova entry in `config/indicators.yaml` + logica in `indicators.py` (AST interpreter) +
  dot/pill nel frontend con verdict + rule_matched come gli 8 indicatori esistenti
- Dipendenza: solo dati già presenti (T+RH realtime). Nessuna fonte esterna fragile

### P9 — Temporale nei prossimi 30-60 min (nowcast Blitzortung)

**Decisione**: Blitzortung (fulmini real-time free) come fonte scelta per il nowcast
"temporale in arrivo". Mantiene l'architettura attuale (4 NWP Open-Meteo + obs SIR/Netatmo
+ ML LightGBM) intatta per forecast e realtime; Blitzortung aggiunge solo il segnale
anticipatorio mancante (precursore canonico del temporale, 30-60 min prima della cella).

**Alternative scartate e perché**:
- **Parsing tile PNG di RainViewer**: fragile, bandwidth, complessità
- **Tomorrow.io Free Plan**: vendor lock-in, validazione NASA limitata a CONUS, incoerente
  con l'architettura "4 NWP + obs + ML" (sostituirebbe il modello proprietario interno).
  Resta opzione per usi non-core futuri (fallback vento o altri parametri opzionali)
- **Heuristic realtime** (∆p Netatmo, salto vento SIR, spike RH): orizzonte 0-15 min,
  troppo tardi per "30-60 min"

**Implementazione**:
- API: `https://data.blitzortung.org/Data/Protected/lightning.json` (free, no auth per
  query basse; rate limit ~1 query/5s)
- Strategia: strikes ultimi 30-60 min nel raggio di 50km dalla location; se presenti,
  ETA = distanza del più vicino / velocità tipica (~40 km/h)
- Output JSON: `"storm_approaching": {"eta_min": 25, "intensity": "light"|"moderate"|"heavy"}`
  in `current`
- Indicatore DLE opzionale (stile "panni") con semaforo allerta

**Limitazione accettata**: Blitzortung copre solo temporali con fulmini, non pioggia
generica. Per pioggia nei prossimi 30-60 min senza temporale non c'è alternativa
accettabile libera. Se il caso d'uso si amplia, riaprire la discussione.

### P10 — Allerte meteo Protezione Civile (da allertameteo.app)

- **Decisione**: allertameteo.app (community, free, no key) come fonte scelta per le allerte
  meteo ufficiali. Mantiene l'architettura "4 NWP + obs + ML" intatta; aggiunge solo
  il segnale "allerta" che non esiste oggi nel prodotto
- **Cosa fornisce**: allerte per oggi e domani su 4 livelli (verde/giallo/arancione/rosso),
  per 3 tipologie di rischio: idraulico, temporali, idrogeologico
- **API**: `https://www.allertameteo.app/api/alert/{codice_istat_comune}` — JSON, free,
  no auth, no rate limit. Endpoint metadata: `/api/regioni`, `/api/province`, `/api/comuni`,
  `/api/zone`. Storico: `/api/storico/download`
- **Sorgente dati**: i bollettini sono sincronizzati dal repo ufficiale `pcm-dpc/IT-alert-Hub`
  e dai Centri Funzionali regionali, qualità alta; servizio terze parti senza SLA
- **Prerequisito**: codici ISTAT comuni per le 6 location in `config/locations.yaml`
  (es. Scandicci 048041, Prato 100005, Firenze 048017, Sesto Fiorentino 048043 — da verificare)
- **Output JSON** (nuovo campo in `current`):
  ```json
  "alert": {
    "today": {"level": "giallo", "risks": {"idraulico": "...", "temporali": "...", "idrogeologico": "..."}},
    "tomorrow": {"level": "arancione", "risks": {...}},
    "source": "allertameteo.app",
    "bulletin_date": "2026-07-29",
    "bulletin_time": "14:32"
  }
  ```
- **Indicatore DLE opzionale** (stile "panni"): semaforo 4 colori basato sul livello
  massimo fra oggi/domani, con verdict testuale ("Stai in casa" per arancione+, ecc.)
- **Schedule**: 1 fetch ogni 6h è sufficiente (bollettino emesso 1 volta/giorno, con
  aggiornamenti durante eventi). Schedulabile in coda alla `pipeline 6h`
- **Rischio accettato**: allertameteo.app è singolo developer, niente SLA. **Fallback**:
  DPC repo GitHub `pcm-dpc/DPC-Bollettini-Criticita-Idrogeologica-Idraulica` (PDF/ZIP
  ufficiale, serve parser) — da implementare solo se allertameteo.app sparisce
- **Complementare a P9**: P9 (Blitzortung) = nowcast breve fulmini; P10 (allertameteo)
  = allerte ufficiali 24-48h ahead. Insieme coprono "sta arrivando" + "è previsto"

## Roadmap sprint

| Sprint | Stato | Contenuto |
|---|---|---|
| 8 | ✅ Completato | Deploy homelab (k3s, Flux, PVC, CronJob, Tailscale Funnel da riportare) |
| 9 | 🔜 | Calibrazione soglie DLE (30-60gg `indicator_log` in produzione) |
| 10 | 🔜 | Nowcasting orario (richiede 6-12 mesi di `realtime` in produzione) |
| 11 | 🔜 | Case study / pubblicazione (repo pubblico, articolo) |
| 12 | 🔜 | Google Air Quality + Pollen API (hourly) — aggiunge sezione polline |

## Note tecniche operative

### SIR — endpoint CSV (download.php)

| Sensore | IDST |
|---|---|
| termometro | `termo_csv` |
| pluviometro | `pluvio0_24` |
| igrometro | `igro0_24` |
| anemometro | `anemo0_24` |
| idrometro | `idro_l` |

barometro, radiometro_*, evaporimetro: **solo realtime**, nessuno storico CSV.

### SIR — Realtime (actions.php)

Richiede `X-Requested-With: XMLHttpRequest` **e** `Referer: https://www.sir.toscana.it/`.
Senza Referer risponde con HTML (redirect al portale).

### granularity in PK observations

`PRIMARY KEY (source, station_id, ts, granularity)` — necessario perché SIR storico
scrive sempre `ts=00:00:00`; un realtime a mezzanotte produrrebbe la stessa PK.
Valori: `daily`, `realtime`, `hourly` (riservato).

### Timestamp convention

Tutte le osservazioni in `observations` sono **UTC naive**.
`forecasts` rimane UTC-aware (i NWP ragionano in UTC).
`observations` daily/label-di-giorno non convertite per convenzione (non sono istanti).

### DuckDB in k8s

- PVC `ReadWriteOnce`, `storageClassName: local-path` (NVMe)
- `concurrencyPolicy: Forbid` su tutti i CronJob writer
- Backup = `cp` snapshot in CronJob dedicato

### deploy/nginx-k8s.conf

Porta 8080, pid `/tmp/nginx.pid`, log su `/dev/stdout|stderr`,
`location /data/` → alias PVC `/var/lib/guazza/output/`, endpoint `/health`.

### Skill metriche baseline (walk-forward CV 4 fold, 2023-01 → 2026-06)

| Target | MAE | Coverage 80% | Coverage 90% | Skill vs NWP-mean |
|---|---|---|---|---|
| tmin_c | 0.850°C | 0.788 | 0.905 | +15.6% |
| tmax_c | 0.813°C | 0.810 | 0.898 | +42.6% |
| precip_mm | 1.545mm | 0.814 | 0.903 | −2.9% |

Backtest multi-lead D+0→D+7: Guazza batte NWP a ogni lead (tmin +13…+33%, tmax +5…+13%).
Dettaglio in `docs/decisions.md` §D-016.
