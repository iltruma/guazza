# Output JSON — contract obbligatorio

Riferimento on-demand. La regola sempre-attiva ("ogni previsione è una distribuzione,
mai valori puntuali nudi") è in `CLAUDE.md`.

File: `data/output/{location_id}.json` (uno per location, sovrascritto ad ogni run di
`predict`). Struttura multi-giorno: ogni file contiene la striscia `days` da D+0 a D+7.

> **Nota**: `data/` è escluso dal `.claudeignore`, quindi questi file non sono leggibili
> via Read/Grep. Per ispezionare un output reale usare Bash (`jq`/`cat`).

```json
{
  "location_id": "casa_campi",
  "generated_at": "2026-05-18T...",
  "coverage_empirical_30d": {
    "tmin_ci80": float | null, "tmin_ci90": float | null,
    "tmax_ci80": float | null, "tmax_ci90": float | null,
    "precip_ci80": float | null, "precip_ci90": float | null
  },
  "current": {"ts": str, "ts_sir": str | null, "ts_netatmo": str | null,
              "temp_c": float, "humidity_pct": float, "precip_mm": float,
              "wind_speed_ms": float | null, "wind_dir_deg": float | null,
              "dewpoint_c": float, "feels_like_c": float,
              "pressure_hpa": float | null,
              "weather_code": int | null},
  "air_quality": {"pm10_ugm3": float | null, "pm25_ugm3": float | null,
                  "no2_ugm3": float | null, "o3_ugm3": float | null,
                  "co_mgm3": float | null, "benzene_ugm3": float | null,
                  "so2_ugm3": float | null},
  "nwp_models_hourly": [{"source": str, "label": str, "data": [{...}]}],
  "days": [
    {
      "target_date": "2026-05-19",
      "lead_time_h": 24,
      "weather_code": int | null,
      "forecasts": {
        "tmin_c":    {"p50": float, "ci80_lo": float, "ci80_hi": float, "ci90_lo": float, "ci90_hi": float},
        "tmax_c":    {"p50": float, ...},
        "precip_mm": {"p50": float, ...}
      },
      "indicators": {
        "panni":    {"verdict": "verde|giallo|rosso|grigio", "rule_matched": "green|yellow|red|fallback|unknown"},
        "motorino": {"verdict": "...", "rule_matched": "..."}
      },
      "hourly": [{...}],
      "nwp_comparison": [{"source": str, "label": str, "tmin_c": float,
                          "tmax_c": float, "precip_mm": float, "last_run": str}]
    }
  ]
}
```

- `coverage_empirical_30d`: rolling 30 giorni predictions vs obs. `null` se < 10 campioni → dashboard mostra "calibrazione in corso".
- `current` e `air_quality` sono `null` se non ci sono osservazioni recenti (rispettivamente realtime meteo e ARPAT).
- `current.ts` è il timestamp più recente del blend (UTC, suffisso `Z`); `ts_sir` è il MIN tra le stazioni SIR (freshness onesta del dato osservativo), `ts_netatmo` il MAX dei moduli Netatmo. Entrambi `null` se la sorgente non contribuisce (es. fallback NWP, o location senza SIR realtime).
- `current.pressure_hpa` è la pressione di superficie da Open-Meteo (non SIR) — può essere `null` se non ci sono dati NWP recenti.
- `current.weather_code` è il codice WMO modale (moda tra modelli NWP nell'ora più vicina a now) — può essere `null` se non ci sono dati NWP recenti.
- `days[].weather_code` è il codice WMO modale giornaliero (moda su 24h × N modelli, ultimo run per fonte) — `null` se assente.
- `nwp_models_hourly[].data[].weather_code` è il codice WMO per quell'ora e modello — `int | null`.

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

## Decision Logic Engine — logging obbligatorio

Ogni invocazione DLE deve produrre log in DuckDB (`indicator_log`):

```python
{"ts": datetime, "location_id": str, "indicator_id": str,
 "input_summary": dict, "rule_matched": str, "verdict": str, "probability": float}
```
