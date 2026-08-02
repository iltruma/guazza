# Guazza — Known Issues

> Workaround non ovvi e comportamenti anomali documentati.
> Formato: `KI-NNN — Titolo` con severità, stato, e workaround.

---

## KI-021 — Qualità aria: serve `weights refresh` (stazioni ARPAT risolte via station_weights)

**Severità**: media (config-step obbligatorio per nuove location)
**Stato**: risolto in codice; richiede `weights refresh` dopo modifiche al config ARPAT

`get_current_air_quality` risolve le stazioni ARPAT via JOIN `station_weights`
(`source='arpat'`), **non** via `observations.location_id`. Motivo: la PK di
`observations` è `(source, station_id, ts, granularity)` — non include
`location_id`. Una stazione ARPAT condivisa tra più location (es. FI-MOSSE usata
da casa_nicco e casa_cercina) viene riscritta in upsert con un solo `location_id`
arbitrario (l'ultimo ingest vince). La query precedente, filtrando su
`obs.location_id`, perdeva l'AQ per la location "perdente" ed era una race tra
location vicine.

**Conseguenza operativa**: dopo aver aggiunto/modificato `arpat_stations` in
`locations.yaml`, eseguire `weights refresh` — popola i pesi `source='arpat'` in
`station_weights`. Senza, `air_quality` è `null` per tutte le location (la JOIN
non trova pesi). È lo stesso meccanismo già usato dal SIR (vedi `get_current_conditions`).

## KI-022 — Target di training corrotto: `obs_weighted` joinava anche su `location_id`

**Severità**: alta (target ML errato per le stazioni condivise — scoperto 2026-06-05)
**Stato**: risolto (`features.py`, fix + rebuild + retrain 2026-06-05)

Stesso pattern di KI-021/KI-020 ma sul percorso del **target di training**, dove è
sfuggito più a lungo. `obs_weighted` in `features.py` joinava `observations` e
`station_weights` su `station_id` **e** `location_id`. Ma `station_weights` è la mappa
autorevole stazione→location (una stazione pesa su più location); le obs sono salvate
sotto una sola `location_id` "home" (la PK di `observations` non include `location_id`).
Il join scartava quindi i contributi delle stazioni condivise.

**Sintomo estremo**: `lavoro_cosimo` aveva `target_tmin_c` **nullo al 100%** — la sua
stazione primaria TOS01001215 ha le obs salvate sotto `casa_campi`/`casa_nicco`, mai
`lavoro_cosimo`. Il modello non veniva mai addestrato per quella location. `lavoro_madda`
aveva il target tmin sistematicamente **−2.09°C** rispetto alla primaria (blend con mix
di stazioni non voluto). 8 stazioni totali col mismatch.

**Scoperto** dal robustness check `analysis/skill_vs_primary.py` (D-016): lo skill ML vs
gauge primario risultava net negativo su tmin (−31%), tracciato fino a questo bug.

**Fix**: join solo su `station_id`, `GROUP BY sw.location_id` — identico a
`ring_precip_raw` poco sotto. Nessun doppio conteggio (una riga daily per stazione/giorno).
Dopo rebuild+retrain: copertura target a ~99% per tutte le location; skill vs gauge
primario tmin +8%, tmax +26% (vs target pesato +17/+45%, prima gonfiati anche dal target
corrotto).

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

## KI-017 — OpenAQ: copertura ridotta vs ARPAT diretto (AR-ENELSB-SANGIOVANNI, FI-LAVAGNINI)

**Severità**: informativa
**Stato**: risolto — cutover OpenAQ → ARPAT NRT OpenData completato 2026-05-22

Il ritorno al fetch diretto ARPAT OpenData NRT recupera entrambe le stazioni
mancanti: AR-ENELSB-SANGIOVANNI (BENZENE, CO per casa_cesto) e FI-LAVAGNINI
(NO2 per casa_nicco). Vedi KI-018 per le note sul nuovo endpoint.

---

## KI-018 — Cutover OpenAQ → ARPAT NRT OpenData: righe storiche source='openaq' nel DB

**Severità**: informativa
**Stato**: risolto (2026-06-27, v0.11.2)

Il fetcher OpenAQ è stato rimosso (2026-05-22). Le righe `source='openaq'` in
`observations` sono ormai orfane. Lo storico AQ non serve (`get_current_air_quality()`
usa finestra 3h, AQ non è feature di training).

**Pulizia eseguita** (verificata 2026-06-27): `SELECT COUNT(*) FROM observations
WHERE source='openaq'` → 0 righe. Niente da cancellare localmente. Stessa
situazione attesa sul server homelab (k3s+Flux con PVC nuovo ricrea il DB pulito al primo
deploy).

**Note sul design ARPAT NRT OpenData** (apprese durante l'implementazione 2026-05-22):
- Endpoint: `https://opendata.arpat.toscana.it/.../json_orari_nrt/{STATION_ID}/{DD-MM-YYYY}`
- Formato risposta: lista di dict orari con `ORA` ("00"-"23"), `DATA_OSSERVAZIONE` ("22-MAY-26")
  e parametri come valori numerici o null. Mesi in inglese (`MAY`, non `MAG`).
- Parametri mappati: PM10, PM2.5, NO2, O3, CO (mg/m³, D.Lgs.155/2010), SO2, BENZENE/C6H6.
- Parametri non mappati ignorati: H2S, BC, BB.
- Timestamp già in ora locale italiana (naive, coerente con SIR e DuckDB).
- `station_id` nel DB = ID ARPAT puro (`FI-FIGLINE`) — nessun suffisso location.

## KI-016 — Cutover ARPAT → OpenAQ: righe storiche source='arpat' nel DB

**Severità**: informativa
**Stato**: risolto — DELETE eseguito in locale il 2026-05-20; cutover completamente
revertito il 2026-05-22 con KI-018.

Lo storico qualità aria non serve: `get_current_air_quality()` usa una finestra 3h,
l'AQ non è feature di training. Le 23.218 righe ARPAT (vecchio fetcher) erano già
state cancellate. Vedi KI-018 per la pulizia delle righe OpenAQ.

---

## KI-011 — ecmwf_aifs025 restituisce null su tutte le variabili via Open-Meteo

**Severità**: informativa (modello rimosso dallo stack)
**Stato**: risolto — modello escluso da `_OM_MODELS`

**Problema**: Open-Meteo espone `ecmwf_aifs025` come modello disponibile, ma
restituisce `null` per tutte le variabili orarie (temperatura, precipitazione,
umidità, vento). Verificato su più location e date nel maggio 2026.
Il modello è probabilmente non ancora integrato nell'archivio Historical
Forecast API di Open-Meteo, o ha un nome diverso internamente.

**Risoluzione**: `ecmwf_aifs025` rimosso da `_OM_MODELS` in `fetchers.py`.
Non incluso nel pivot `features_daily`. Eventuali righe già presenti nel DB
con `source = 'open_meteo_ecmwf_aifs025'` hanno tutte le colonne metriche
a NULL e possono essere cancellate (vedi nota DB sotto).

**Nota DB**: se il backfill storico è già stato eseguito con AIFS attivo,
eliminare le righe con:
```sql
DELETE FROM forecasts WHERE source = 'open_meteo_ecmwf_aifs025';
```
La pulizia è consigliata ma non bloccante — le righe null non influenzano
il training perché il pivot SQL non referenzia quella sorgente.

---

## KI-001 — SIR realtime richiede header Referer

**Severità**: alta (blocca scraping se assente)
**Stato**: workaround stabile

**Problema**: l'endpoint `actions.php` del portale SIR Toscana risponde con
redirect HTML al portale se manca l'header `Referer`.

**Workaround**: aggiungere sempre entrambi gli header:
```python
headers = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.sir.toscana.it/",
}
```

**Nota**: questo comportamento non è documentato nell'API SIR. Potrebbe
cambiare senza preavviso a seguito di aggiornamenti del portale.

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

## KI-003 — DuckDB non supporta scritture concorrenti

**Severità**: media (rilevante se due cron si sovrappongono)
**Stato**: risolto in `storage.py`

**Problema**: DuckDB file-based non supporta scritture concorrenti da più
processi.

**Fix**: `DuckDBClient.__enter__` acquisisce un `fcntl.flock(LOCK_EX)` sul file
`.lock` prima di aprire il DB. Un secondo processo si blocca in attesa del lock
invece di crashare con `IOException`. Il DB non può corrompersi.

---

## KI-004 — Open-Meteo rate limit non documentato

**Severità**: media (blocca il backfill `historical`)
**Stato**: da monitorare

**Problema**: Open-Meteo free tier non documenta esplicitamente il rate limit.

- **Forecast API** (`api.open-meteo.com`): job `forecasts` con 4 location ×
  6 modelli (batch coordinate → 1 richiesta per modello) resta ampiamente sotto
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

## KI-006 — Netatmo token scadenza silente

**Severità**: bassa
**Stato**: risolto

**Problema**: il token OAuth Netatmo ha scadenza. Se scade, le chiamate API
restituiscono HTTP 401/403 senza messaggio chiaro.

**Soluzione**: refresh automatico implementato in `fetchers.py`. `_call_with_refresh`
intercetta 401/403, chiama `_refresh_token` (grant `refresh_token`) e riscrive
`NETATMO_ACCESS_TOKEN`/`NETATMO_REFRESH_TOKEN` nel `.env`, poi ritenta la chiamata.

**Prerequisito**: `NETATMO_REFRESH_TOKEN` e `NETATMO_CLIENT_ID`/`CLIENT_SECRET`
devono essere presenti nel `.env`.

---

## KI-008 — DuckDB: DELETE su tabella con indice grande fallisce

**Severità**: media (operazione una-tantum, non impatta produzione)
**Stato**: workaround stabile

**Problema**: `DELETE FROM forecasts WHERE source = '...'` su tabelle con molte
righe e PRIMARY KEY fallisce con `FATAL Error: Failed to delete all rows from index`.
Bug noto DuckDB con ART index e grandi eliminazioni.

**Workaround**:
```python
c.execute('CREATE TABLE forecasts_keep AS SELECT * FROM forecasts WHERE source != <da_eliminare>')
c.execute('DROP TABLE forecasts')
# Ricrea con schema completo (PRIMARY KEY + indici)
c.execute('INSERT INTO forecasts SELECT * FROM forecasts_keep')
c.execute('DROP TABLE forecasts_keep')
```

**Nota**: applicato il 2026-05-16 per eliminare 153.248 righe `open_meteo_ecmwf_ifs025`.

---

## KI-009 — DuckDB: TIMESTAMPTZ → TIMESTAMP usa timezone locale, causa duplicati DST

**Severità**: alta (constraint error che blocca `ingest historical`)
**Stato**: risolto in `storage.py`

**Problema**: `_staging_forecasts` usava colonne `TIMESTAMPTZ` mentre `forecasts` usa
`TIMESTAMP` (naive). DuckDB converte TIMESTAMPTZ → TIMESTAMP usando il timezone di
sessione (Europe/Rome). Nei giorni di transizione DST (es. 2024-10-27), due UTC distinti
(00:00Z e 01:00Z) collassano entrambi in `02:00:00` locale. Il ROW_NUMBER dedup
nella staging non li intercettava (opererebbe su TIMESTAMPTZ distinti); `INSERT OR REPLACE`
riceveva due righe con la stessa PK naive → `Constraint Error: Duplicate key`.

**Fix**: in `upsert_forecasts`, i datetime UTC-aware vengono normalizzati a UTC-naive
con `.replace(tzinfo=None)` prima di entrare nella staging. La staging ora usa `TIMESTAMP`.
Il dedup Python e quello SQL operano sulla stessa rappresentazione finale.

**Side effect**: la tabella `forecasts` pre-fix conteneva timestamp in ora locale Italia
(non UTC). La tabella è stata ricreata vuota e ri-popolata via `ingest historical --only-openmeteo`.

---

## KI-010 — upsert_forecasts lento su batch grandi (backfill storico)

**Severità**: bassa (impatta solo il backfill one-shot, non la produzione)
**Stato**: risolto in `storage.py`

**Problema**: `upsert_forecasts` e `upsert_sir_observations` usavano `executemany`
per caricare la staging table. Su batch da ~38.000 righe (4.4 anni di dati orari)
impiega ~70 secondi → ~550 rec/sec.

**Fix**: sostituito CREATE TEMP TABLE + `executemany` con `pd.DataFrame` +
`conn.register(name, df)` in entrambe le funzioni. DuckDB usa il path Arrow
vectorized — atteso 10–50x speedup su batch grandi. Logica UPDATE/INSERT
invariata; rimossa solo la staging table fisica.

---

## KI-012 — Indicatore `bisenzio` fallback giallo: soglie idrometriche non popolate

**Severità**: bassa (indicatore di sola allerta, non previsionale)
**Stato**: risolto (Pre-Sprint 6, 2026-05-18)

Soglie statiche inserite in `config/indicators.yaml` (threshold_1=3.5m, threshold_2=5.5m,
threshold_3=7.0m). `evaluate_indicator` inietta `cfg["thresholds"]` nel SignalBag prima
dell'eval — indicatore sbloccato dal fallback giallo.

**Workaround attuale**: fallback "giallo" è conservativo — non peggio di "non so".

**Soluzione pianificata (Sprint 7-8)**:
- Opzione A — configurazione manuale in `config/indicators.yaml` per TOS01004791
  (S. Piero a Ponti): ricavare le soglie dai bollettini di allerta CFR/SIR.
- Opzione B — aggiungere `threshold_1`/`threshold_2` al SignalBag in `build_signals()`
  leggendole da una sezione `thresholds` di `config/locations.yaml`.

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

## KI-013 — Timestamp SIR realtime: naive CEST salvato senza timezone

**Severità**: bassa (impatto solo display frontend, non training)
**Stato**: risolto (D-017, 2026-05-30)

**Problema**: il fetcher SIR realtime salva i timestamp delle osservazioni come
`datetime` naive in Python, che DuckDB tratta come ora locale CEST. Il backend
in `get_current_conditions` aggiungeva il suffisso `+00:00` al timestamp
formattato, facendo credere al browser JS che fosse UTC → conversione a CEST
→ display con +2h di scarto.

**Workaround iniziale** (2026-05-18): rimosso `|| '+00:00'` dal `strftime` in
`get_current_conditions`. Il timestamp viene restituito come stringa ISO naked;
`new Date("...T17:15:00")` senza suffisso viene interpretato come ora locale
dal browser (corretto).

**Risoluzione definitiva** (D-017, 2026-05-30): standardizzazione in **UTC naive**
per tutte le osservazioni nel DB. SIR realtime/bulk convertono CEST→UTC
(`-1h`), ARPAT NRT (hourly) converte locale→UTC, Netatmo invariato.
`get_current_conditions` ora formatta con suffisso `Z` (`%Y-%m-%dT%H:%M:%SZ`)
e il frontend lo interpreta come UTC esplicito. Niente più workaround,
tutto coerente con la convenzione UTC.

**Conseguenza residua**: c'è una discrepanza di ~0-60 minuti tra il timestamp
mostrato e l'ora locale attesa, causata dal fatto che SIR aggiorna i dati con
qualche minuto di ritardo e/o usa boundary temporali (es. ultima ora intera).
Non è un bug del codice — è la latenza nativa del sistema SIR.

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

## KI-015 — pressure_hpa nel pannello realtime può essere null

**Severità**: bassa (informativa — solo display)
**Stato**: by design

`current.pressure_hpa` è la pressione di superficie aggregata dalle osservazioni
realtime (`granularity='realtime'`, finestra 3h). Le stazioni SIR e Netatmo non
riportano la pressione; il valore viene solo da Open-Meteo quando il job `realtime`
inserisce le sue osservazioni sintetiche. Se il job non è stato eseguito di recente
il campo è `null` e la 4a cella della stats grid mostra `—`.

## KI-023 — Drift di calibrazione CQR rilevato in walk-forward CV

**Severità**: media (risolto con ACI in Sprint 9)
**Stato**: risolto (`AdaptiveConformalizer` in `models.py`, v0.10.0 — 2026-06-27)

Walk-forward CV 4 fold, 2023-01 → 2026-06, mostra drift di calibrazione CQR nei fold
recenti (2025-2026):

| Target | Coverage 80% target | Coverage 80% effettivo | Drift |
|---|---|---|---|
| tmin_c | 0.80 | 0.688 | −11pp |
| tmax_c | 0.80 | 0.699 | −10pp |

Il calibration set statico (364 righe, feb-mag 2026) non è rappresentativo dei dati di
produzione futuri. Il drift è di calibrazione, non di accuratezza (MAE stabile).

**Risoluzione**: Adaptive Conformal Inference (Gibbs & Candès 2021) in `models.py`.
`AdaptiveConformalizer` aggiusta α_t online sulle coppie (prediction, actual), garantendo
copertura long-run marginal anche sotto distribution shift. Cold start N=30 (CQR statico
fino a 30 osservazioni). Persistenza in DuckDB (`aci_state`). Dettaglio in D-019.

## KI-024 — Spike anomaly target: degradato in walk-forward CV (+28/+44% MAE)

**Severità**: bassa (spike documentato, non in produzione)
**Stato**: disattivato (2026-06-27), candidato per retry con climatologia raffinata

**Prova**: walk-forward CV 4 fold, 2023-01 → 2026-06, 6 modelli NWP.

| Target | Baseline (status.md) | Anomaly | Δ MAE |
|---|---|---|---|
| tmin_c | 0.850 | 1.089 | **+28%** |
| tmax_c | 0.813 | 1.168 | **+44%** |
| precip_mm | 1.545 | 1.717 | +11% (precip NON era in ANOMALY_TARGETS — il +11% è effetto collaterale: training set ridotto per tmin/tmax quando `clim_tmin_mean` è NULL, leggera instabilità CV) |

Soglia di accettazione +3% MAE tmin/tmax: non centrata, rollback eseguito.

**Causa probabile**: la climatologia usata (`clim_tmin_mean`, mensile, aggregata su 4 anni) è troppo "grezza" per essere un buon anchor di anomalia. La media mensile smussa la variabilità settimanale, e sui 4 anni del training è dominata da 2-3 stagionalità recenti climaticamente non rappresentative. Il modello impara l'anomalia ma non ha feature `anom_*_c` in `FEATURE_COLS` per collegarla al NWP, quindi deve re-imparare il livello assoluto dalle stesse feature che usava prima, con perdita netta.

**Side-finding emerso dal test**: `coverage_80` nei fold recenti (2025-2026) è 0.688/0.699 su tmin/tmax (target 0.80) — **drift di calibrazione CQR già in atto** sui dati di produzione. Vedi KI-023; risolto con ACI in Sprint 9 (v0.10.0).

**Cosa fare se si vuole ritentare**:
1. Sostituire `clim_tmin_mean` mensile con climatologia settimanale percentile (10/50/90) calcolata su tutti gli anni SIR (2004+) invece dei soli 4 anni del training
2. Aggiungere `anom_tmin_c`/`anom_tmax_c` direttamente in `FEATURE_COLS` (oggi sono calcolate ma non lette dal modello)
3. Ripetere la misurazione: se Δ MAE ancora negativo, lasciare perdere l'anomalia come target

**Stato corrente**: `ANOMALY_TARGETS = ()` in `features.py` (disattivato). Codice in `models.py` (`_target_col`, `_invert_anomaly`, campo `anomaly_targets` in `TrainingArtifacts`) tenuto come regression test + punto di partenza per retry futuro. Colonne `anom_*` rimosse da `features_daily` (ALTER TABLE 2026-06-27).

**Nessun fix pianificato**: dipende dalla disponibilità del dato a monte.

## KI-025 — GFS ha record orari senza `temp_c` valorizzato (~6.7% del totale)

**Scoperto** durante il debug di `skill_history_daily` (Sprint 11, 2026-06-27):
il backtest grafico GFS è completamente vuoto. Indagando:
- `forecasts` ha 246786 record per `source='open_meteo_gfs025'`
- Solo 16440 (6.7%) hanno `temp_c` NON NULL
- Anche `precip_mm` è NULL sul ~90% dei record (24072/246786)

**Causa probabile**: l'API Open-Meteo per GFS potrebbe aver cambiato i parametri
della risposta oraria, oppure il fetcher `fetch_openmeteo_forecast_batch` non
sta estraendo correttamente i campi per GFS. Da investigare confrontando la
risposta API live con i record attuali nel DB.

**Effetto pratico**:
- Il backtest grafico (`affidabilita.html` sezione "Come ha performato") non
  mostra GFS perché tutti i valori sono NULL. Il frontend nasconde
  automaticamente le source con tutti null nella finestra corrente.
- Le 5 NWP restanti (ECMWF IFS, ICON-EU, ICON-D2, AROME France, ARPAE ICON-2I)
  funzionano regolarmente e coprono il backtest.

**Mitigazione provvisoria** (nel frontend): `affidabilita.js` filtra i NWP
che hanno almeno un valore non-null nella finestra corrente prima di
disegnarli. GFS semplicemente non appare finché il problema non è risolto.

**Fix pianificato**:
1. Verificare la risposta API Open-Meteo per GFS oggi (endpoint + parametri)
2. Verificare la query in `fetch_openmeteo.py` per l'estrazione di temp_c/precip_mm
3. Se l'API è cambiata, aggiornare la query; altrimenti ri-ingesta GFS

**Stato**: aperto. Non bloccante per il deploy (Sprint 8) né per la pagina
affidabilità (funziona con 5 NWP).
