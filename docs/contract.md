# Output JSON — contract obbligatorio

Riferimento on-demand. La regola sempre-attiva ("ogni previsione è una distribuzione,
mai valori puntuali nudi") è in `AGENTS.md`.

File: `data/output/{location_id}.json` (uno per location, sovrascritto ad ogni run di
`predict`). Struttura multi-giorno: ogni file contiene la striscia `days` da D+0 a D+7.

> **Nota**: `data/` non è leggibile via Read/Grep. Per ispezionare un output reale usare
> Bash (`jq`/`cat`).

```json
{
  "location_id": "casa_campi",
  "generated_at": "2026-05-18T...",
  "updates": {"pipeline_at": "UTC ISO-8601" | null, "realtime_at": "UTC ISO-8601" | null},
  "coverage_empirical_30d": {
    "tmin_ci80": float | null, "tmin_ci90": float | null,
    "tmax_ci80": float | null, "tmax_ci90": float | null,
    "precip_ci80": float | null, "precip_ci90": float | null
  },
  "current": {"ts": str, "ts_sir": str | null, "ts_netatmo": str | null,
              "temp_c": float, "humidity_pct": float, "precip_mm": float,
              "wind_speed_ms": float | null, "wind_dir_deg": float | null,
              "wind_speed_source": "realtime" | "nwp" | null,
              "dewpoint_c": float, "feels_like_c": float,
              "pressure_hpa": float | null,
              "weather_code": int | null,
              "sources": {"temp_c": "realtime"|"nwp"|null,
                          "humidity_pct": "realtime"|"nwp"|null,
                          "precip_mm": "realtime"|"nwp"|null,
                          "wind_speed_ms": "realtime"|"nwp"|null,
                          "wind_dir_deg": "realtime"|"nwp"|null,
                           "pressure_hpa": "realtime"|"nwp"|null,
                           "weather_code": "realtime"|"nwp"|null}},
   "nwp_models_hourly": [{"source": str, "label": str, "data": [{...}]}],
  "days": [
    {
      "target_date": "2026-05-19",
      "lead_time_h": 24,
      "weather_code": int | null,
      "forecasts": {
        "tmin_c":    {"p50": float, "ci80_lo": float, "ci80_hi": float, "ci90_lo": float, "ci90_hi": float},
        "tmax_c":    {"p50": float, ...},
        "precip_mm": {"mean": float, "p50": float, "ci80_lo": float, "ci80_hi": float, "ci90_lo": float, "ci90_hi": float}
      },
      "indicators": {
        "panni":    {"verdict": "verde|giallo|rosso|grigio", "rule_matched": "green|yellow|red|fallback|unknown", "rule_text": str},
        "motorino": {"verdict": "...", "rule_matched": "...", "rule_text": str}
      },
      "hourly": [
        {
          "hour": int (0-23),
          "temp_c": float | null,
          "temp_ci80_lo": float | null,
          "temp_ci80_hi": float | null,
          "humidity_pct": float | null,
          "precip_mm": float | null,
          "precip_ci80_lo": float | null,
          "precip_ci80_hi": float | null,
          "precip_prob": float | null,
          "wind_speed_ms": float | null,
          "weather_code": int | null
        }
      ],
      "nwp_comparison": [{"source": str, "label": str, "tmin_c": float,
                          "tmax_c": float, "precip_mm": float, "last_run": str}]
    }
  ]
}
```

- `coverage_empirical_30d`: rolling 30 giorni predictions vs obs. `null` se < 10 campioni → dashboard mostra "calibrazione in corso".
- `generated_at` è il timestamp UTC ISO di generazione della pipeline: è lo stesso valore di `updates.pipeline_at` (compatibili e uguali).
- `updates`: metadata temporale di scrittura del file — `pipeline_at` = completamento dell'ultima pipeline (== `generated_at`), `realtime_at` = completamento dell'ultimo patch realtime del JSON (`guazza-ingest realtime`). `realtime_at` è il momento del refresh, NON il timestamp dell'osservazione: quelli sono `current.ts_sir`/`current.ts_netatmo`. JSON legacy senza `updates` vengono normalizzati a `{"pipeline_at": null, "realtime_at": <refresh>}` al primo refresh.
- `current` è `null` se non ci sono osservazioni recenti.
- `current.ts` è il timestamp più recente del blend (UTC, suffisso `Z`); `ts_sir` è il MIN tra le stazioni SIR (freshness onesta del dato osservativo), `ts_netatmo` il MAX dei moduli Netatmo. Entrambi `null` se la sorgente non contribuisce (es. fallback NWP, o location senza SIR realtime).
- `current.sources`: provenance per-variabile dei valori raw in `current` — `"realtime"` se la singola variabile viene dal blend SIR/Netatmo, `"nwp"` se è stata ripiegata sul forecast NWP (fallback per-variabile, fix P3), `null` se il valore è assente. `pressure_hpa` e `weather_code` sono sempre `"nwp"` quando valorizzati (oggi arrivano solo da forecasts). `dewpoint_c`/`feels_like_c` sono derivati e non hanno marker.
- `current.wind_speed_source`: alias retrocompatibile di `sources["wind_speed_ms"]` — provenienza della velocità del vento mostrata: `"realtime"` se da osservazioni SIR/Netatmo realtime, `"nwp"` se ripiegata sul forecast NWP (fix P3: obs valide ma senza anemometro o dato realtime non arrivato), `null` se il vento manca del tutto. Il frontend usa il valore per segnalare visivamente i dati non osservativi.
- `current.pressure_hpa` è la pressione di superficie da Open-Meteo (non SIR) — può essere `null` se non ci sono dati NWP recenti.
- `current.weather_code` è il codice WMO modale (moda tra modelli NWP nell'ora più vicina a now) — può essere `null` se non ci sono dati NWP recenti.
- `days[].weather_code` è il codice WMO modale giornaliero (moda su 24h × N modelli, ultimo run per fonte) — `null` se assente.
- `nwp_models_hourly[].data[].weather_code` è il codice WMO per quell'ora e modello — `int | null`.
- `precip_mm.mean` è il valore atteso E[precip] della distribuzione (utile per valutazione economica/rischio). Solo per `precip_mm` — `tmin_c`/`tmax_c` non espongono `mean`.
- `days[].indicators.*.rule_text` è il testo della regola YAML che ha prodotto il verdetto, per debugging e trasparenza.
- ~~`days[0].intraday`~~ rimosso 2026-06-27: la correzione aritmetica D+0 di Tmin/Tmax con le osservazioni SIR realtime generava valori assurdi in assenza di letture notturne (Tmin = 36°C di pomeriggio). Le card `tmin_c`/`tmax_c` per D+0 sono ora la previsione ML pura, identica al grafico orario. Gli **indicatori DLE** (panni, motorino, gelata) continuano a usare `build_signals_today()` con realtime (decisione D-015).
- `days[].hourly[].temp_ci80_lo/hi` e `precip_ci80_lo/hi` sono le **bande di confidenza orarie CI 80%** derivate per interpolazione dal forecast daily (rescaling dello stesso profilo NWP grezzo con bound `tmin_c.ci80_lo/hi`, `tmax_c.ci80_lo/hi`, `precip_mm.ci80_lo/hi`). Sono `null` se i bound daily sono assenti (es. cold-start CI o modello NWP). Le bande orarie non esistono per il vento (solo `wind_speed_ms` puntuale). Il frontend le usa per disegnare la fascia d'incertezza nei grafici daily/weekly (toggle "Banda CI 80%").

## `skill.json` — curva di skill (file globale)

File separato `data/output/skill.json`, **uno solo** (non per-location): generato dal job
`guazza-skill` (`jobs/skill.py`), letto dal frontend per la sezione "Quanto è affidabile".
Misura retrospettiva MAE Guazza vs consensus NWP per orizzonte D+0…D+7, contro il
**termometro SIR primario** di ogni location (verità indipendente, non il target pesato).

```json
{
  "generated_at": "2026-06-05T...Z",
  "ground_truth": "sir_primary",
  "window_start": "2025-10-15",
  "window_end": "2026-06-05",
  "embargo_days": 7,
  "leads_h": [0, 24, 48, 72, 96, 120, 144, 168],
  "min_samples_per_lead": 5,
  "locations": {
    "casa_cercina": {
      "sir_station_id": "TOS01001215",
      "tmin_c": [{"lead_h": 0, "n": 232, "mae_nwp": 1.85, "mae_ml": 1.46, "skill_pct": 21.1}, ...],
      "tmax_c": [{...}, ...]
    }
  }
}
```

- Solo `tmin_c` e `tmax_c`: la MAE su precip è troppo rumorosa per una curva pulita.
- Un punto con `n < min_samples_per_lead` ha `mae_*`/`skill_pct` a `null` (campione insufficiente).
- `skill_pct = (1 − mae_ml/mae_nwp)·100`; negativo = Guazza peggiora il NWP su quella location/lead.
- `window_end` è l'ultima data con osservazione SIR reale (le date forecast future sono escluse).
- Finestra limitata dall'archivio `previous_dayN` di Open-Meteo (~ott 2025→oggi): è una
  finestra di mesi, non lifetime — il frontend lo dichiara esplicitamente.

## `skill_history.json` — time series forecast vs actual (file globale)

File separato `frontend/data/skill_history.json`, **uno solo**: generato come
passo 4 della `pipeline.py` (6h). Misura **per ogni giorno
passato** come hanno performato i vari modelli sul forecast emesso a D-1 (lead
24h) per D, rispetto al valore osservato. Popolamento incrementale (append
giornaliero, idempotente).

```json
{
  "generated_at": "2026-06-27T...Z",
  "lead_h": 24,
  "sources": [
    "guazza",
    "open_meteo_ecmwf_ifs",
    "open_meteo_icon_eu",
    "open_meteo_gfs025",
    "open_meteo_arome_france",
    "open_meteo_italia_meteo_arpae_icon_2i"
  ],
  "variables": ["tmin_c", "tmax_c", "precip_mm"],
  "min_date": "2026-05-28",
  "max_date": "2026-06-03",
  "locations": {
    "casa_campi": {
      "tmin_c": {
        "dates": ["2026-05-28", "2026-05-29", ...],
        "actual": [18.6, 17.7, ...],
        "guazza": [null, 17.5, 15.4, ...],
        "open_meteo_ecmwf_ifs": [19.1, 18.0, 15.9, ...],
        "open_meteo_icon_eu": [...],
        ...
      },
      "tmax_c": {...},
      "precip_mm": {...}
    },
    ...
  }
}
```

- **Lead fisso a 24h** (forecast emesso a D-1 per D). La colonna `lead_h` esiste
  nella tabella `skill_history_daily` per future estensioni multi-lead.
- **Allineamento date**: per ogni location, le date sono l'unione delle date
  con `actual` valorizzato in `obs_weighted_daily`. Le entry senza forecast da
  un dato source sono `null` in quel campo (es. NWP down, Guazza non ancora
  trainato per quella location).
- **Actual**: `obs_weighted_daily` (stessa vista usata da training e indicatori).
- **Forecast Guazza**: `predictions` con `lead_time_h BETWEEN 23 AND 25`,
  mediane (p50).
- **Forecast NWP**: aggregazione daily di `forecasts` orari con lead 23-25h
  (`MIN(temp_c) AS tmin_c, MAX(temp_c) AS tmax_c, SUM(precip_mm) AS precip_mm`).
  Stessa logica del CTE `daily_nwp` in `features.py` ma per singolo source.
- **NWP senza dati**: GFS rimosso dal setup in v0.11.1 (KI-025); le righe
  storiche restano in `forecasts` come dati morti ma innocui. Il frontend
  nasconde i NWP con tutti valori null nella finestra corrente.
- **Job**:
  - `append [--day YYYY-MM-DD | --days N]` (default: ieri): ~21 righe × N
    location × N giorni. Idempotente (PK composta + ON CONFLICT DO UPDATE).
  - `dump [--output PATH]` (default `frontend/data/skill_history.json`):
    scrittura atomica, aggrega la tabella in JSON.
- **Schedule k8s proposta**: `15 6 * * *` UTC per `append` (15 min dopo
  `daily` ingest), `30 6 * * *` per `dump`.

## Decision Logic Engine — logging obbligatorio

Ogni invocazione DLE deve produrre log in DuckDB (`indicator_log`):

```python
{"ts": datetime, "location_id": str, "indicator_id": str,
 "input_summary": dict, "rule_matched": str, "verdict": str, "probability": float,
 "alpha": float, "cost_fn": float, "cost_fp": float, "last_modified": datetime}
```
