# Guazza — Known Issues

> Workaround non ovvi e comportamenti anomali documentati.
> Formato: `KI-NNN — Titolo` con severità, stato, e workaround.

---

## KI-017 — OpenAQ: copertura ridotta vs ARPAT diretto (AR-ENELSB-SANGIOVANNI, FI-LAVAGNINI)

**Severità**: informativa
**Stato**: by design (limitazione upstream OpenAQ)

**Problema**: dopo il cutover ARPAT → OpenAQ (KI-016), due delle 10 stazioni
ARPAT precedentemente usate non risultano aggregate da OpenAQ:

| Stazione | Location | Parametri persi | Note |
|---|---|---|---|
| AR-ENELSB-SANGIOVANNI | casa_cesto | BENZENE, CO | Stazione industriale ENEL, ~8km |
| FI-LAVAGNINI | casa_nicco | NO2 | Firenze centro |

**Impatto**:
- **casa_nicco**: nessuno effettivo — NO2 resta coperto da FI-MOSSE (1.1km),
  FI-GRAMSCI (3.7km), FI-BASSI (4.2km, nuova stazione non presente in ARPAT
  config) e FI-SCANDICCI (5.5km). Anzi, FI-BASSI aggiunge SO2 e PM2.5.
- **casa_cesto**: l'unica stazione OpenAQ nel raggio 15km è FI-FIGLINE (3.7km,
  solo NO2 e PM10), che aggiorna in modo intermittente. BENZENE e CO non
  sono più disponibili. La sezione qualità aria può mostrare valori vuoti
  quando FI-FIGLINE non aggiorna da >3h.

**Decisione**: non reimplementare il fetcher ARPAT NRT diretto. Costo (due
sorgenti AQ eterogenee, API ARPAT non documentata, logica di merge in
`get_current_air_quality()`) sproporzionato rispetto al beneficio (una sola
location, parametri non critici per indicatori DLE).

**Workaround frontend**: `renderAirQuality()` mostra sempre tutti e 7 i
parametri AQ — i valori non disponibili compaiono come `—`, evitando il
mismatch visivo tra location coperte e non coperte.

---

## KI-016 — Cutover ARPAT → OpenAQ: righe storiche source='arpat' nel DB

**Severità**: informativa
**Stato**: risolto — DELETE eseguito in locale il 2026-05-20

Lo storico qualità aria non serve: `get_current_air_quality()` usa una finestra 3h,
l'AQ non è feature di training. Le 23.218 righe ARPAT sono state cancellate.

Sul VPS (Sprint 7), prima di avviare i cron, eseguire la stessa pulizia se il DB
è stato copiato da locale:

```sql
DELETE FROM observations WHERE source = 'arpat';
DELETE FROM quality_flags
  WHERE flag_type IN ('range_pm10_high','range_pm25_high','range_no2_high','range_o3_high');
```

Dopodiché il cron `realtime` (ogni 30 min) popola automaticamente i dati OpenAQ.
Nessun backfill storico necessario.

**Note sul design OpenAQ** (apprese durante l'implementazione 2026-05-20):
- `/locations/{id}/latest` restituisce `sensorsId` (int), **non** include
  `parameter`. Il mapping `sensor_id → (param, units)` va costruito dalla
  discovery `/locations?coordinates=...`.
- `station_id` nel DB è `openaq_{id}_{location_id}` (non solo `openaq_{id}`):
  la stessa stazione fisica può cadere nel raggio di più location e la PK
  `(source, station_id, ts, granularity)` non include location_id.
- Timestamp OpenAQ convertiti da UTC a ora locale naive (Europe/Rome) prima
  del salvataggio, coerente con SIR e con `CURRENT_TIMESTAMP` di DuckDB
  (che usa il timezone della macchina).

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
  da un IP diverso (es. direttamente sul VPS in fase di deploy).

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

**Soluzione pianificata (Sprint 7+)**:
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
**Stato**: workaround stabile (2026-05-18)

**Problema**: il fetcher SIR realtime salva i timestamp delle osservazioni come
`datetime` naive in Python, che DuckDB tratta come ora locale CEST. Il backend
in `get_current_conditions` aggiungeva il suffisso `+00:00` al timestamp
formattato, facendo credere al browser JS che fosse UTC → conversione a CEST
→ display con +2h di scarto.

**Workaround**: rimosso `|| '+00:00'` dal `strftime` in `get_current_conditions`.
Il timestamp viene restituito come stringa ISO naked; `new Date("...T17:15:00")`
senza suffisso viene interpretato come ora locale dal browser (corretto).

**Conseguenza residua**: c'è una discrepanza di ~0-60 minuti tra il timestamp
mostrato e l'ora locale attesa, causata dal fatto che SIR aggiorna i dati con
qualche minuto di ritardo e/o usa boundary temporali (es. ultima ora intera).
Non è un bug del codice — è la latenza nativa del sistema SIR.

---

## KI-014 — Vento realtime quasi sempre null (Netatmo base, stazioni SIR senza anemometro)

**Severità**: bassa (informativa — non impatta training né indicatori DLE)
**Stato**: by design / limitazione hardware

**Problema**: il pannello "Condizioni attuali" mostra `—` per il vento nella
maggior parte delle location. Le stazioni Netatmo base non montano il modulo
anemometro; le stazioni SIR a volte lo hanno ma i dati realtime non sempre sono
disponibili.

**Conseguenza**: `P(wind > 40kmh)` e `P(wind < 5kmh)` nel SignalBag vengono
calcolati dal NWP ensemble (non da obs realtime) anche con `build_signals_today`.
Per gli indicatori `motorino` (vento < 5 km/h) e simili, questo introduce una
discrepanza tra display realtime e logica DLE.

**Nessun fix pianificato**: limitazione hardware/dati a monte.
