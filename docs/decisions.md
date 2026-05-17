# Guazza — Decisioni architetturali

> Questo file documenta le decisioni tecniche e scientifiche con motivazione.
> Ogni voce ha: contesto → opzioni considerate → scelta → ragionamento → conseguenze.

---

## D-001 — ERA5 mai come predittore di forecast

**Contesto**: ERA5 è una reanalisi ECMWF ad alta risoluzione spaziotemporale,
spesso usata come ground truth in letteratura ML meteo.

**Problema**: ERA5 assimila osservazioni reali → è una stima del vero stato
atmosferico. In produzione si hanno solo forecast NWP (ECMWF, ICON-EU, GFS)
che non hanno visto la verità. Usare ERA5 come feature di input → train/serve
skew grave → metriche gonfiate in CV, degrado reale in produzione.

**Usi consentiti**:
- Climatologia statica: media/std mensile multi-decennale come feature
- Ground truth alternativo per location senza stazioni SIR
- Backfill storico: solo come *target* (osservazione), mai come predittore

**Se ERA5 appare come input dinamico a un modello → è un bug.**

**Riferimenti**: Rasp & Lerch 2018 (NN post-processing), Glahn & Lowry 1972
(MOS originale), Taillardat et al. 2019 (quantile regression forests).

---

## D-002 — Embargo 7 giorni in cross-validation

**Contesto**: i fold temporali standard (es. TimeSeriesSplit di sklearn)
non garantiscono separazione tra train e validation per serie meteorologiche.

**Problema**: l'autocorrelazione sinottica è tipicamente 5-7 giorni. Senza
embargo, esempi correlati finiscono in train e validation → metriche gonfiate.

**Scelta**: embargo minimo 7 giorni tra fine train e inizio validation in ogni
fold. Implementazione: `TimeSeriesSplit` con `gap=7*24` (ore) se granularità
oraria, `gap=7` se giornaliera.

**Conseguenze**: N fold effettivi ridotto, ma metriche oneste.

**Riferimenti**: Roberts & Chatfield 2006, Bergmeir et al. 2018.

---

## D-003 — CQR stratificato per lead time bucket

**Contesto**: Conformal Quantile Regression (Romano et al. 2019) garantisce
copertura marginale sul calibration set. Ma l'errore di forecast cresce con
il lead time.

**Problema**: un singolo modello CQR produce CI troppo stretti per t+48h e
troppo larghi per t+1h.

**Scelta**: calibration set separato per 5 bucket di lead time:
- `0-6h`, `6-12h`, `12-24h`, `24-48h`, `48-72h`

Un set di residui (e quindi di quantili conformali) per bucket.

**Conseguenze**: 5× la dimensione del calibration set necessario; minimo
~200 esempi per bucket per coverage stabile.

---

## D-004 — `coverage_empirical_30d` obbligatorio nel JSON di output

**Contesto**: CI calibrati con CQR hanno garanzia teorica, ma su microclimi
toscani non validati il coverage reale può discostarsi.

**Scelta**: ogni output JSON include:
```json
{"coverage_empirical_30d": {"temp_ci80": 0.81, "temp_ci90": 0.88}}
```
Rolling window 30 giorni su osservazioni vs CI. Se dati insufficienti → `null`,
dashboard mostra "calibrazione in corso".

**Motivazione**: onestà scientifica. Il CI non è "magicamente" calibrato;
mostrare quanto lo è nella pratica recente.

---

## D-005 — Modello globale con location-id categorica

**Contesto**: 4 location, dati storici limitati per location.

**Opzioni**:
1. 4 modelli indipendenti per location
2. 1 modello globale con `location_id` come feature categorica

**Scelta**: opzione 2.

**Motivazione**: dati storici per-location sono pochi (anni, non decenni).
Il modello globale trasferisce informazione tra location. LightGBM gestisce
natively le categoriche. Se una location ha dati migliori, contribuisce di
più senza danneggiare le altre.

**Conseguenza**: serve `location_id` come categoria in ogni riga del training
set. Niente one-hot encoding.

---

## D-006 — DuckDB file-based, schema ricostruibile

**Contesto**: alternativa era PostgreSQL o SQLite.

**Scelta**: DuckDB con `schema.sql` come unico source of truth. Nessun sistema
di migrations. Il file `.duckdb` è ricostruibile in <1 ora da Parquet raw.

**Motivazione**: single-node, no query concorrenti in scrittura, backup = `cp`.
Eliminare migrations riduce complessità mantenendo semplicità di recovery.

**Conseguenza**: `schema.sql` va tenuto aggiornato. Ogni modifica schema =
ricreazione del DB in staging + test che passano.

---

## D-007 — Stack blindato (no orchestration layer)

**Contesto**: vedi AGENTS.md sezione "Anti-pattern".

**Motivazione sintetica**: progetto single-node, 1 utente, budget €3.79/mese.
Ogni layer aggiuntivo (Prefect, Docker, FastAPI 24/7) aggiunge:
- superficie di failure
- complessità di debug
- costo operativo

**Scelta**: cron Linux + Python scripts. Stupido, robusto, prevedibile.
Nessuna eccezione ammessa senza bug tecnico documentato che la imponga.

---

## D-008 — Schema wide (non EAV)

**Contesto**: le osservazioni meteo hanno molti sensori (temp, humidity,
precip, wind, pressure, ecc.) con sparsità variabile per stazione.

**Opzioni**:
1. EAV (Entity-Attribute-Value): una riga per sensore
2. Wide: una riga per `(source, station_id, ts)`, colonne sparse

**Scelta**: wide.

**Motivazione**: DuckDB è column-oriented → le colonne NULL non costano.
Le query analitiche (join, aggregazioni) su schema wide sono 10-100× più
semplici e veloci che su EAV. LightGBM si aspetta feature come colonne.

**Conseguenza**: colonne `NULL` per sensori non disponibili su certa stazione.
Query di aggregazione con `COALESCE` / `NULLIF` dove necessario.

---

## D-009 — Indicatori operativi come prodotto primario

**Contesto**: l'output finale è una previsione meteo per uso personale.

**Decisione**: gli indicatori operativi (panni, motorino, gelata, ecc.)
non sono feature secondarie. Sono il prodotto che l'utente vede ogni giorno.
La pipeline ML è strumentale a produrli bene.

**Conseguenza**: la Decision Logic Engine (DLE) è un componente di prima
classe con logging strutturato obbligatorio in `indicator_log`.

---

## D-010 — Niente valori puntuali nudi

**Decisione**: ogni previsione è una distribuzione. Ogni output include CI.
Un valore puntuale senza CI è un bug di output, non una feature.

**Motivazione**: le previsioni sono stime probabilistiche. Presentarle come
certezze è scientificamente disonesto e operativamente fuorviante.

---

## D-011 — Ottimizzazione fetch Open-Meteo via coordinate batching

**Data**: 2026-05-16

**Contesto**: il download sequenziale per location/modello (24+ chiamate)
risultava lento e incline a latenze elevate, specialmente nel job historical.

**Decisione**: sfruttare la capacità di Open-Meteo di gestire liste di
coordinate per scaricare i dati di tutte le location in una sola chiamata per
modello.

**Motivazione**: riduzione drastica del numero di richieste HTTP (da N_location
a 1 per modello), minor rischio di throttling, velocità ~4-5x.

---

## D-012 — Temporal chunking per backfill storico

**Data**: 2026-05-16

**Contesto**: le richieste alla Historical Forecast API per lunghi periodi
(2+ anni) e modelli ad alta risoluzione (AROME, ICON-D2) causano timeout o
errori 400 per la dimensione eccessiva dell'elaborazione server-side.

**Decisione**: frazionamento automatico delle richieste in chunk (180 giorni
per i modelli globali, 90 giorni per icon_d2 e arome_france).

**Motivazione**: stabilità del download senza sovraccaricare l'API; recupero
parziale in caso di fallimenti isolati di un singolo intervallo.

---

## D-013 — Selezione modelli NWP per post-processing iper-locale

**Data**: 2026-05-16

**Contesto**: la risoluzione spaziale dei modelli globali (25 km) è
insufficiente per catturare i microclimi toscani complessi (es. inversione
termica nella piana di Campi o Scandicci).

**Decisione**:
- Sostituire `ecmwf_ifs025` (25 km) con `ecmwf_ifs` (HRES, 9 km). Stessa fisica, orografia molto più dettagliata.
- Aggiungere `icon_d2` (2.2 km). Modello convective-permitting del DWD che copre il Centro-Nord Italia, fondamentale per la dinamica locale.
- Scartare `ecmwf_aifs025`: Open-Meteo restituisce null per tutte le variabili (vedi KI-011).
- Mantenere `arome_france` (2.5 km) e `icon_eu` (7 km) come modelli ad alta risoluzione.
- Aggiungere `italia_meteo_arpae_icon_2i` (2.2 km, ItaliaMeteo/ARPAE): unico modello che assimila osservazioni italiane, orizzonte 72h.

**Conseguenza**: il dataset di training `features_daily` usa 6 modelli NWP
(ecmwf_ifs, icon_eu, icon_d2, gfs025, arome_france, icon_2i).
Necessario rieseguire il backfill `historical` per i nuovi modelli.
