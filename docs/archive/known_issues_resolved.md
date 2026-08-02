# Guazza — Known Issues (archivio risolti)

> Workaround storici e bug chiusi. Per i KI attivi vedi `docs/known_issues.md`.
> Formato: `KI-NNN — Titolo` con severità, stato, e workaround.

---

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

## KI-017 — OpenAQ: copertura ridotta vs ARPAT diretto (AR-ENELSB-SANGIOVANNI, FI-LAVAGNINI)

**Severità**: informativa
**Stato**: risolto — cutover OpenAQ → ARPAT NRT OpenData completato 2026-05-22

Il ritorno al fetch diretto ARPAT OpenData NRT recupera entrambe le stazioni
mancanti: AR-ENELSB-SANGIOVANNI (BENZENE, CO per casa_cesto) e FI-LAVAGNINI
(NO2 per casa_nicco). Vedi KI-018 per le note sul nuovo endpoint.

## KI-016 — Cutover ARPAT → OpenAQ: righe storiche source='arpat' nel DB

**Severità**: informativa
**Stato**: risolto — DELETE eseguito in locale il 2026-05-20; cutover completamente
revertito il 2026-05-22 con KI-018.

Lo storico qualità aria non serve: `get_current_air_quality()` usa una finestra 3h,
l'AQ non è feature di training. Le 23.218 righe ARPAT (vecchio fetcher) erano già
state cancellate. Vedi KI-018 per la pulizia delle righe OpenAQ.

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

## KI-003 — DuckDB non supporta scritture concorrenti

**Severità**: media (rilevante se due cron si sovrappongono)
**Stato**: risolto in `storage.py`

**Problema**: DuckDB file-based non supporta scritture concorrenti da più
processi.

**Fix**: `DuckDBClient.__enter__` acquisisce un `fcntl.flock(LOCK_EX)` sul file
`.lock` prima di aprire il DB. Un secondo processo si blocca in attesa del lock
invece di crashare con `IOException`. Il DB non può corrompersi.

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

## KI-013 — Timestamp SIR realtime: naive CEST salvato senza timezone

**Severità**: bassa (impatta solo display frontend, non training)
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
(`-1h`), Netatmo invariato.
`get_current_conditions` ora formatta con suffisso `Z` (`%Y-%m-%dT%H:%M:%SZ`)
e il frontend lo interpreta come UTC esplicito. Niente più workaround,
tutto coerente con la convenzione UTC.

**Conseguenza residua**: c'è una discrepanza di ~0-60 minuti tra il timestamp
mostrato e l'ora locale attesa, causata dal fatto che SIR aggiorna i dati con
qualche minuto di ritardo e/o usa boundary temporali (es. ultima ora intera).
Non è un bug del codice — è la latenza nativa del sistema SIR.

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

---

## KI-025 — GFS ha record orari senza `temp_c` valorizzato (~6.7% del totale)

**Severità**: bassa
**Stato**: risolto per rimozione (GFS rimosso dallo stack in v0.11.1, 2026-06-27)

**Problema originale** (2026-06-27, debug di `skill_history_daily`):
- `forecasts` aveva 246786 record per `source='open_meteo_gfs025'`
- Solo 16440 (6.7%) avevano `temp_c` NON NULL
- Anche `precip_mm` NULL sul ~90% dei record (24072/246786)
- Causa probabile: l'API Open-Meteo per GFS aveva cambiato parametri, oppure
  il fetcher `fetch_openmeteo_forecast_batch` non li estraeva correttamente.
- Effetto: backtest grafico GFS vuoto in `affidabilita.html` sezione "Come ha performato".
  Frontend filtrava automaticamente le source con tutti null.

**Risoluzione**: GFS rimosso completamente dallo stack (5 → 4 NWP: ECMWF IFS,
ICON-EU, AROME France, ARPAE ICON-2I) in v0.11.1. Le 246k righe GFS già presenti
in `forecasts` sono state lasciate come dati morti ma innocui (per non rompere audit).
Rimosso anche `_OM_PREVIOUS_DAY_MAX` e l'elenco da `NWP_MODEL_PREFIXES`,
`OM_MODELS`, `NWP_SOURCES`, `NWP_LABELS` (frontend). Dettaglio in `CHANGELOG.md` [0.11.1].

**Workaround che era in uso**: il frontend `affidabilita.js` filtrava i NWP con
almeno un valore non-null nella finestra corrente prima di disegnarli. GFS
semplicemente non appariva. Reso obsoleto dalla rimozione del modello.
