# Guazza — Known Issues

> Workaround non ovvi e comportamenti anomali documentati.
> Formato: `KI-NNN — Titolo` con severità, stato, e workaround.

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
**Stato**: da ottimizzare

**Problema**: `upsert_forecasts` usa `executemany` per caricare la staging table.
Su batch da ~38.000 righe (4.4 anni di dati orari per un model+location) impiega
~70 secondi → ~550 rec/sec. DuckDB dovrebbe stare sui 50.000–200.000 rec/sec.
Il bottleneck è il bridge Python-oggetti → DuckDB, che bypassa il path Arrow.

**Fix pianificato**: sostituire `executemany` con `conn.register(name, df)` dove
`df` è un `pd.DataFrame`. DuckDB usa Arrow internamente e può essere 10–50x più
veloce su batch grandi.

```python
df = pd.DataFrame(rows, columns=[...])
self._conn.register("_staging_forecasts", df)
# poi lo stesso INSERT OR REPLACE
self._conn.unregister("_staging_forecasts")
```

**Workaround**: nessuno necessario — il backfill storico si esegue una volta sola
e 8 minuti totali sono accettabili. Il job `daily` (24 rec/call) non è affetto.

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
