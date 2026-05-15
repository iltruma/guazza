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

## KI-007 — SIR `pluvio0_24`: finestra 08:00–08:00 CEST, non mezzanotte UTC

**Severità**: media (impatta allineamento con Open-Meteo e feature engineering)
**Stato**: confermato empiricamente

**Problema**: il CSV storico SIR (`pluvio0_24`) aggrega la precipitazione sulla
finestra **08:00–08:00 ora locale** (CEST = UTC+2 in estate). La riga con data
`YYYY-MM-DD` contiene la pioggia caduta tra le 06:00 UTC del giorno corrente
e le 06:00 UTC del giorno successivo.

**Esempio osservato** (2026-05-14, TOS01001215 Scandicci):
- CSV storico `pluvio0_24`: `0,0 mm` (flag `P` = prevalidato)
- Portale SIR realtime: 2.2 mm cumulativi giornalieri
- Spiegazione: i 2.2 mm sono caduti dopo le 06:00 UTC del 14/05, quindi
  ricadono nella riga del **15/05** nel CSV storico.

**Conseguenza per il feature engineering**:
- Il join `observations.precip_mm` (SIR, finestra 08-08) ↔ `forecasts.precip_mm`
  (Open-Meteo, finestra 00-00 UTC) introduce un offset sistematico di ~6h.
- Per il training LightGBM: usare `precip_mm` SIR come target giornaliero
  richiede di allineare la finestra (es. aggregare le 24h di forecast dalla
  06:00 UTC, non dalla 00:00 UTC).

**Workaround**: in fase di feature engineering, shiftare la finestra di
aggregazione delle precipitazioni forecast di +6h per allinearla alla
finestra SIR. Documentare esplicitamente nell'artefatto di training.
