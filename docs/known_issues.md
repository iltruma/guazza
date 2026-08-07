# Guazza — Known Issues

> Workaround attivi e comportamenti anomali non risolti. Per i KI chiusi consultare
> `docs/archive/known_issues_resolved.md` (storia intatta per riferimento futuro).
> Formato: `KI-NNN — Titolo` con severità, stato, e workaround.

---

## KI-002 — Sensori SIR senza storico CSV

**Severità**: informativa
**Stato**: by design (limitazione upstream)

barometro, radiometro_*, evaporimetro: solo realtime (`actions.php`), niente
`download.php`. Colonne `pressure_hpa` / `radiation_wm2` / `evaporation_mm` in
`observations` popolate solo su righe realtime. Non usarle come feature senza
gestire la sparsità storica.

---

## KI-004 — Open-Meteo rate limit non documentato

**Severità**: media (blocca il backfill `historical`)
**Stato**: da monitorare

**Problema**: Open-Meteo free tier non documenta esplicitamente il rate limit.

- **Forecast API** (`api.open-meteo.com`): job `forecasts` con 4 location ×
  4 modelli (batch coordinate → 1 richiesta per modello) resta ampiamente sotto
  soglia. Nessun problema osservato.
- **Historical Forecast API** (`historical-forecast-api.open-meteo.com`): rate
  limit per IP molto più aggressivo. Il backfill `historical` (2022→oggi)
  genera ~70 richieste a causa del temporal chunking (D-012) e satura la quota.
  Una volta scattato il limite, il 429 arriva immediatamente sulla prima
  richiesta — non è una questione di frequenza intra-run ma di quota IP
  cumulativa. Le risposte 429 **non** includono `Retry-After`.

**Workaround**:
- `tenacity` con backoff già configurato (`_fetch_om_json_historical`: 5
  tentativi, `_wait_historical` rispetta `Retry-After` se presente).
- Se l'IP è bloccato: attendere il reset della finestra o eseguire il backfill
  da un IP diverso (es. direttamente sul server homelab in fase di deploy).

**Nota**: WSL in modalità NAT condivide l'IP dell'host Windows — un backfill
fallito in locale brucia la quota per l'intera macchina.

---

## KI-014 — Vento realtime spesso null (hardware)

**Severità**: bassa (informativa — non impatta training né indicatori DLE)
**Stato**: residuo by design (fix stazioni condivise 2026-05-29)

Netatmo base non ha anemometro; alcune SIR non pubblicano vento realtime.
`get_current_conditions` fa media pesata via `station_weights` (JOIN su
`station_id`) e riempie `wind_*` mancanti con fallback NWP (`sources=nwp`,
asterisco in UI). Di conseguenza `P(wind > 40kmh)` / `P(wind < 5kmh)` nel
SignalBag usano l'ensemble NWP anche in `build_signals_today` — discrepanza
display realtime vs logica DLE per indicatori vento-dipendenti (es. motorino).
Bug stazioni condivise taggate con un solo `location_id`: risolto, vedi KI-020.

---

## KI-015 — pressure_hpa nel pannello realtime può essere null

**Severità**: bassa (informativa — solo display)
**Stato**: by design

`current.pressure_hpa` viene sempre dai `forecasts` NWP (media nella finestra
~now), non da SIR/Netatmo. Se non ci sono forecast recenti con pressione il
campo è `null` e la cella «Pressione» della hero stats mostra `—`
(`frontend/app.js`). Nessun impatto su training o DLE.

---

## KI-019 — SIR download.php: rate-limit per IP ~4 s/req

**Severità**: informativa (limite upstream, non aggirabile)
**Stato**: documentato — convivere col limite

Dopo la 1ª richiesta da un IP "fresco" (~150 ms), `download.php` serializza
per IP a ~4 s TTFB. Diagnostica 2026-05-26: nessun client-side workaround
(UA, TLS impersonation, HTTP/2, cookie) riduce il TTFB. Implicazioni:
~28 combo station×sensor → ~120 s wall-clock irriducibili sul path storico;
fetch sequenziale obbligatorio (`_ingest_sir_historical_range`, niente
`max_workers>1`). Proxy multi-IP fuori scope / ToS. Non sostituire httpx.

---

## KI-020 — Remediation `predictions.*_obs` (stazioni condivise)

**Severità**: bassa
**Stato**: remediation pendente; wind_dir circolare risolto (2026-08-07)

`get_current_conditions` / backfill pesano via `station_weights` (JOIN
`station_id`). Residuo unico:

1. **Remediation `predictions.*_obs`** (zona rossa — scrittura DuckDB):
   `backfill_prediction_obs` aggiorna solo `tmin_obs IS NULL`. Righe
   backfillate con la vecchia logica (JOIN `location_id`) restano stale su
   location con stazioni condivise (`casa_nicco`, `lavoro_cosimo`), salvo
   reset DB. Una-tantum:
   ```sql
   UPDATE predictions SET tmin_obs = NULL, tmax_obs = NULL, precip_obs = NULL
   WHERE location_id IN ('casa_nicco', 'lavoro_cosimo');
   ```
   poi `predict` / backfill. Mostrare e confermare prima di eseguire.
   Se il DB prod viene ricreato da zero (coda status), questo punto decade.

**Risolto (2026-08-07)**: la media scalare di `wind_dir_deg` (wraparound
0/360 errato: 350°+10° → ~180° invece di ~0°) è stata sostituita con la media
circolare `atan2(Σw·sinθ, Σw·cosθ)` + wrap `+360 % 360`, con guard `COUNT` su
wind_dir non-null (niente NULLIF su x/y: y=0 è normale con venti cardinali).
Applicata a `_BLEND_SQL` e al fallback NWP in `db_queries.py`. Test wraparound
(350+10 → 0, blend e fallback) in `test_output.py`.

---
