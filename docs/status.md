# Guazza — Stato corrente

> Aggiornato: 2026-07-29 (v0.12.0)
> Storico sprint → `CHANGELOG.md`

## Stato

| | |
|---|---|
| Versione | **0.12.0** |
| Test | 337 verdi (suite completa; `test_models.py` ~3min per LightGBM training) |
| Lint / mypy | ✅ puliti |
| Deploy | Sprint 8 in corso — k3s homelab (`houston`, namespace `guazza`) |

## Architettura corrente

| Componente | Stato |
|---|---|
| Pipeline 6h | `guazza-pipeline` — forecasts → features → predict+DLE+JSON → skill-history → monitor |
| Ingest | `guazza-ingest historical/daily/realtime` (SIR + Open-Meteo + Netatmo + ARPAT) |
| Modello | LightGBM quantile + CQR + ACI (AdaptiveConformalizer, Gibbs & Candès 2021) |
| NWP | 5 modelli: ECMWF IFS, ICON-EU, ICON-D2, AROME France, ICON-2I |
| Location | 6: casa_campi, lavoro_cosimo, lavoro_madda, casa_cesto, casa_nicco, casa_cercina |
| Frontend | `index.html` + `affidabilita.html` — CSS custom, Chart.js, Leaflet, RainViewer |
| Schema DB | `schema.sql` unico source of truth (13 tabelle + vista `obs_weighted_daily`) |

## Punti aperti 🟡

### P1 — Retrain post-GFS (azione utente, se non già fatto)

Dopo v0.11.1 (rimozione GFS), il modello ha 25 feature NWP invece di 30.
Il retrain con le feature corrette migliora leggermente lo skill (GFS era rumore).

```bash
uv run guazza-train features build  # oppure guazza-pipeline --dry-run se forecasts ok
uv run guazza-train train run
```

### P2 — ACI in cold start

`AdaptiveConformalizer` entra in warm mode dopo 30 aggiornamenti per (target, lead_bucket).
Fino ad allora `apply_aci_correction` è un pass-through su CQR statico.
Si risolve automaticamente dopo ~30 giorni di operatività. Nessuna azione richiesta.

### P3 — Vento in `current` quasi sempre null

Le stazioni Netatmo base non riportano il vento; solo alcune SIR lo misurano in realtime.
`current.wind_speed_ms` è null sulla maggior parte delle location.
Candidato Sprint 9+ (nowcasting orario usa SIR realtime).

### P4 — `affidabilita.html` come pagina dedicata

Oggi la sezione "Quanto è affidabile" è embeddata nello SPA per-location.
`skill.json` è già globale — candidato a pagina statica separata in Sprint 11 (case study).

### P5 — Backtest multi-anno gated su accumulo forward

`previous_dayN` Open-Meteo parte da ~nov 2025. La versione rigorosa multi-stagione
si accumula solo in avanti dall'avvio in produzione (Sprint 8+).

### P6 — Deploy Sprint 8 in corso

- Manifest k8s in `k8s/apps/guazza/` su repo Houston
- `guazza.it` → DNS Cloudflare (solo zona): CNAME pubblico a `<node>.<tailnet>.ts.net` con HTTPS provisioning Tailscale (`tailscale set --https=guazza.it` sul nodo). `cloudflared` rimosso dallo stack.
- `guazza.lab.paroparo.it` → resta su Traefik (wildcard `*.lab.paroparo.it` già emesso); routing tailnet indipendente da quello pubblico
- SealedSecret `netatmo-credentials` e `healthchecks-url` già nel repo Houston

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

### P9 — Prerequisiti Sprint 12 (Google AQ + Pollen API)

- Account Google Cloud con billing attivato (free tier $200/mese, opzionale alert soglia)
- Air Quality API + Pollen API abilitate nel progetto GCP
- API key in env var `GOOGLE_AQ_API_KEY` + SealedSecret su Houston
- Verifica free tier: 6 location × 24h × 2 API = ~8.6k chiamate/mese, rientra nel credito free
- Specie polline rilevanti per Toscana da coprire: graminacee, parietaria, cipresso, olivo, quercia

## Roadmap sprint

| Sprint | Stato | Contenuto |
|---|---|---|
| 8 | 🔄 in corso | Deploy homelab (k3s, ArgoCD, PVC, Tailscale Funnel) |
| 9 | 🔜 | Calibrazione soglie DLE (30-60gg `indicator_log` in produzione) |
| 10 | 🔜 | Nowcasting orario (richiede 6-12 mesi di `realtime` in produzione) |
| 11 | 🔜 | Case study / pubblicazione (repo pubblico, articolo) |
| 12 | 🔜 | Google Air Quality + Pollen API (hourly) — drop ARPAT, aggiunge sezione polline |

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
