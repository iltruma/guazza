# Guazza — Known Issues

> Workaround non ovvi e comportamenti anomali documentati.
> Formato: `KI-NNN — Titolo` con severità, stato, e workaround.

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
**Stato**: mitigato da design cron serializzato

**Problema**: DuckDB file-based non supporta scritture concorrenti da più
processi. Un secondo processo che tenta di aprire il file in scrittura riceve
`IOException: Could not set lock on file`.

**Workaround attuale**: i cron job sono serializzati (ingest → predict → output)
con slot temporali non sovrapposti. Se un job dura più del previsto, il cron
successivo fallisce senza corrompere il DB.

**Da fare**: aggiungere lock file esterno (`/tmp/guazza.lock`) nei job entry
point per rilevare sovrapposizioni e loggare WARNING invece di crashare con
`IOException`.

---

## KI-004 — Open-Meteo rate limit non documentato

**Severità**: bassa
**Stato**: da monitorare

**Problema**: Open-Meteo API free tier non documenta esplicitamente il rate
limit. In pratica ~600 req/min sembrano sicuri. Con 4 location × 5 modelli NWP
= 20 richieste per run: ampiamente sotto soglia.

**Workaround**: `tenacity` con exponential backoff già configurato. Se si
riceve HTTP 429, il backoff gestisce automaticamente.

---

## KI-005 — CFR Toscana: HTML scraping fragile

**Severità**: media
**Stato**: in monitoring

**Problema**: il portale CFR Toscana non ha API pubblica. Lo scraping è su
HTML che può cambiare senza preavviso.

**Workaround**: parser basato su selettori CSS robusti (tag semantici, non
posizioni). Se il parsing fallisce, il job logga ERROR e continua senza dati
CFR (non blocca l'ingestione SIR/Open-Meteo).

**Segnale di allarme**: se `rows=0` per 3 run consecutivi → controllare
manualmente il portale CFR.

---

## KI-006 — Netatmo token scadenza silente

**Severità**: alta
**Stato**: da implementare

**Problema**: il token OAuth Netatmo ha scadenza. Se scade, le chiamate API
restituiscono HTTP 403 senza messaggio chiaro. Il fetcher attuale non gestisce
il refresh automatico.

**Workaround temporaneo**: rinnovare manualmente il token in `.env` se
`fetch_netatmo_location` inizia a restituire dati vuoti.

**Da fare**: implementare refresh token flow in `fetchers.py::_netatmo_refresh`.

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
