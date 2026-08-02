# Guazza — Known Issues

> Workaround attivi e comportamenti anomali non risolti. Per i KI chiusi consultare
> `docs/archive/known_issues_resolved.md` (storia intatta per riferimento futuro).
> Formato: `KI-NNN — Titolo` con severità, stato, e workaround.

---

## KI-002 — Sensori SIR senza storico CSV

**Severità**: informativa
**Stato**: by design (limitazione upstream)

**Problema**: barometro, radiometro_*, evaporimetro non hanno endpoint
`download.php` per lo storico. Disponibili solo in realtime via `actions.php`.

**Conseguenza**: le colonne `pressure_hpa`, `radiation_wm2`, `evaporation_mm`
in `observations` sono popolate solo per le righe realtime, non per lo storico
SIR.

**Workaround**: nessuno praticabile. Documentato per il feature engineering:
non usare queste colonne come feature senza gestire la sparsità storica.

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

## KI-007 — ECMWF falso allarme precipitazioni su Toscana (osservazione preliminare)

**Severità**: informativa
**Stato**: da verificare su serie storica più lunga

**Osservazione** (2026-05-14, singolo giorno — non conclusiva):
- TOS01001215 Scandicci: 0.0mm osservati, ECMWF 0.7mm, ICON-EU 0.5mm
- TOS11000516 Casa Rota: 0.0mm osservati, ECMWF 2.0mm, ICON-EU 0.4mm

La finestra `pluvio0_24` nel CSV SIR è **mezzanotte–mezzanotte** (confermato
confrontando CSV con dati Excel SIR). I valori CSV sono il riferimento corretto
per il training.

**Nota**: l'ipotesi iniziale di una finestra 08:00–08:00 CEST era errata —
basata su un confronto con cumulativo realtime SIR che usava una finestra
temporale diversa (09:00 del 13/05 → 09:00 del 14/05).

---

## KI-014 — Vento realtime quasi sempre null (Netatmo base, stazioni SIR senza anemometro)

**Severità**: bassa (informativa — non impatta training né indicatori DLE)
**Stato**: parzialmente risolto (2026-05-29) / residuo by design

**Problema**: il pannello "Condizioni attuali" mostra `—` per il vento nella
maggior parte delle location. Due cause distinte:

1. **Stazioni condivise taggate con un'altra location** (risolto 2026-05-29):
   `get_current_conditions` filtrava `observations.location_id`, ma una stazione
   fisica ha una sola riga taggata col primo `location_id` che la usa nel YAML
   (`_location_id_for_station`). Le 4 stazioni anemo condivise tra `lavoro_cosimo`
   e `casa_nicco` finivano taggate `lavoro_cosimo` → `casa_nicco` non vedeva il
   vento. **Fix**: `get_current_conditions` ora fa media pesata via
   `station_weights` (JOIN su `station_id`, non `location_id`) per SIR e via
   `observations.weight` per Netatmo. Vedi [KI-020].
2. **Hardware mancante** (residuo by design): le stazioni Netatmo base non montano
   l'anemometro; alcune SIR non pubblicano il vento realtime.

**Conseguenza** (per il caso 2): `P(wind > 40kmh)` e `P(wind < 5kmh)` nel SignalBag
vengono calcolati dal NWP ensemble (non da obs realtime) anche con
`build_signals_today`. Per `motorino` (vento < 5 km/h) e simili introduce una
discrepanza tra display realtime e logica DLE.

---

## KI-015 — pressure_hpa nel pannello realtime può essere null

**Severità**: bassa (informativa — solo display)
**Stato**: by design

`current.pressure_hpa` è la pressione di superficie aggregata dalle osservazioni
realtime (`granularity='realtime'`, finestra 3h). Le stazioni SIR e Netatmo non
riportano la pressione; il valore viene solo da Open-Meteo quando il job `realtime`
inserisce le sue osservazioni sintetiche. Se il job non è stato eseguito di recente
il campo è `null` e la 4a cella della stats grid mostra `—`.

---

## KI-019 — SIR download.php: rate-limit per IP ~4s/req dopo la 1ª chiamata

**Severità**: informativa (limite architetturale, non aggirabile)
**Stato**: documentato, nessuna azione correttiva possibile

Il server `www.sir.toscana.it/archivio/download.php` applica un throttling
deterministico per IP: la **prima** richiesta da un IP "fresco" risponde in
~150 ms; le **successive** (entro una finestra di N minuti) vengono ritardate
a ~4 s di TTFB indipendentemente da User-Agent, TLS handshake, cookie PHP,
HTTP version o headers usati.

### Diagnostica eseguita (2026-05-26)

Verificato che nessun client-side workaround riduce il TTFB:
- `httpx` con UA Chrome/Firefox/curl/wget → 4 s
- `curl-cffi` con `impersonate=chrome|firefox|safari17_0` → 4 s
- `subprocess curl` (binary di sistema) → 4 s (eccetto 1ª chiamata)
- `urllib` con UA `curl/8.5.0` → 4 s
- HTTP/2, gzip, Referer, Sec-Fetch-*, cookie PHPSESSID reale → nessun effetto

Il browser ottiene 72 ms nell'HAR perché è la 1ª chiamata dopo un cooldown:
ripetendo immediatamente la richiesta, anche il browser sale a ~4 s.

### Implicazioni

- **Daily SIR job**: ~28 combo (station × sensor) × ~4 s = **~120 s wall-clock irriducibili**.
- `ThreadPoolExecutor(max_workers > 1)` **non aiuta**: il server serializza per IP
  e potrebbe applicare throttle più aggressivo. `max_workers=1` è la scelta
  corretta (impostato in `_ingest_sir_historical_range` il 2026-05-26).
- Nessun motivo di sostituire `httpx` con `curl-cffi` o subprocess curl.

### Vie teoriche di bypass (non implementate)

1. Distribuire le richieste su più IP (proxy rotanti) — fuori scope, etica
   discutibile, probabile violazione ToS SIR.
2. Spacing artificiale fra richieste >>10 min — peggiora wall-clock, non
   migliora.

Convivere con il limite è la scelta giusta.

---

## KI-020 — Condizioni attuali: media pesata per distanza (wind_dir scalare, remediation storica)

**Severità**: bassa
**Stato**: documentato (2026-05-29)

`get_current_conditions` e `backfill_prediction_obs` aggregano le osservazioni
pesando per distanza via `station_weights` (JOIN su `station_id`). Due note:

1. **wind_dir media scalare**: la direzione del vento è aggregata con media pesata
   scalare `Σ(dir·w)/Σw`, non con media circolare vettoriale. Vicino al wraparound
   0/360° il risultato è errato (es. 350° e 10° → ~180° invece di 0°). Fix corretto
   (`atan2(Σw·sinθ, Σw·cosθ)`) non implementato per mantenere lo scope contenuto.

2. **Remediation `predictions.*_obs` storici** (zona rossa — scrittura DuckDB):
   le righe già backfillate con la vecchia logica (JOIN su `location_id`) non si
   autoricalcolano (guard `tmin_obs IS NULL`). Per le location con stazioni condivise
   (casa_nicco, lavoro_cosimo) i target possono essere parziali. Correzione una-tantum:
   ```sql
   UPDATE predictions SET tmin_obs = NULL, tmax_obs = NULL, precip_obs = NULL
   WHERE location_id IN ('casa_nicco', 'lavoro_cosimo');
   ```
   poi rieseguire `predict` (o il backfill). Mostrare e confermare prima di eseguire.

---

## KI-024 — Spike anomaly target: degradato in walk-forward CV (+28/+44% MAE)

**Severità**: bassa (spike documentato, non in produzione)
**Stato**: disattivato (2026-06-27), candidato per retry con climatologia raffinata

**Prova**: walk-forward CV 4 fold, 2023-01 → 2026-06, 4 modelli NWP.

| Target | Baseline (status.md) | Anomaly | Δ MAE |
|---|---|---|---|
| tmin_c | 0.850 | 1.089 | **+28%** |
| tmax_c | 0.813 | 1.168 | **+44%** |
| precip_mm | 1.545 | 1.717 | +11% (precip NON era in ANOMALY_TARGETS — il +11% è effetto collaterale: training set ridotto per tmin/tmax quando `clim_tmin_mean` è NULL, leggera instabilità CV) |

Soglia di accettazione +3% MAE tmin/tmax: non centrata, rollback eseguito.

**Causa probabile**: la climatologia usata (`clim_tmin_mean`, mensile, aggregata su 4 anni) è troppo "grezza" per essere un buon anchor di anomalia. La media mensile smussa la variabilità settimanale, e sui 4 anni del training è dominata da 2-3 stagionalità recenti climaticamente non rappresentative. Il modello impara l'anomalia ma non ha feature `anom_*_c` in `FEATURE_COLS` per collegarla al NWP, quindi deve re-imparare il livello assoluto dalle stesse feature che usava prima, con perdita netta.

**Side-finding emerso dal test**: `coverage_80` nei fold recenti (2025-2026) è 0.688/0.699 su tmin/tmax (target 0.80) — **drift di calibrazione CQR già in atto** sui dati di produzione. Vedi KI-023 in archivio (risolto con ACI in v0.10.0).

**Cosa fare se si vuole ritentare**:
1. Sostituire `clim_tmin_mean` mensile con climatologia settimanale percentile (10/50/90) calcolata su tutti gli anni SIR (2004+) invece dei soli 4 anni del training
2. Aggiungere `anom_tmin_c`/`anom_tmax_c` direttamente in `FEATURE_COLS` (oggi sono calcolate ma non lette dal modello)
3. Ripetere la misurazione: se Δ MAE ancora negativo, lasciare perdere l'anomalia come target

**Stato corrente**: `ANOMALY_TARGETS = ()` in `features.py` (disattivato). Codice in `models.py` (`_target_col`, `_invert_anomaly`, campo `anomaly_targets` in `TrainingArtifacts`) tenuto come regression test + punto di partenza per retry futuro. Colonne `anom_*` rimosse da `features_daily` (ALTER TABLE 2026-06-27).

**Nessun fix pianificato**: dipende dalla disponibilità del dato a monte.
