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

**Contesto**: vedi CLAUDE.md sezione "Anti-pattern".

**Motivazione sintetica**: progetto single-node, 1 utente, hardware locale già disponibile (Dell Optiplex Micro 3050), costo infrastruttura ≈ €0.
Ogni layer aggiuntivo (Prefect, Docker, FastAPI 24/7) aggiunge:
- superficie di failure
- complessità di debug
- costo operativo

**Scelta**: i job sono CLI Python idempotenti (`guazza-ingest`, `guazza-predict`, …)
invocabili da qualsiasi scheduler. L'invariante blindato è che l'app resti
**orchestrator-agnostic** — non che si usi per forza `cron`.

Il *target di deploy* è libero (il 3050 è un host Proxmox multi-servizio): cron in una
LXC oppure namespace k8s con CronJob e DB in PVC sono entrambi legittimi. Vietato è
**accoppiare la logica applicativa** a un orchestratore (Prefect/Dagster/Airflow/Celery)
o esporre l'app come PaaS — quello reintroduce superficie di failure e complessità di
debug nel codice. Vedi "Invariante deploy" in CLAUDE.md per i vincoli tecnici DuckDB su k8s.

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

---

## D-014 — Precipitazione: usare ensemble NWP direttamente nel DLE

**Data**: 2026-05-17

**Contesto**: walk-forward CV Sprint 4 mostra skill score precipitazione ≈ 0
(MAE 1.526mm vs NWP ensemble mean 1.57mm). Il post-processing ML non batte il NWP grezzo.

**Cause identificate**:
- Tutti i dati storici hanno `lead_time_h=0` (punto aperto Sprint 3): il modello
  non impara la correzione per lead time, che è dove i NWP sbagliano di più sulla precip.
- Ground truth SIR è una singola stazione pesata — la variabilità spaziale degli eventi
  precipitativi introduce rumore irriducibile non catturabile da un modello globale.
- Distribuzione zero-inflated: LightGBM quantile su dati così asimmetrici richiede
  feature più specifiche (CAPE, theta-e, indici convettivi) non disponibili oggi.

**Decisione**: nel DLE (Sprint 5), per gli indicatori dipendenti dalla pioggia
(`panni`, soglie allerta) usare la distribuzione NWP ensemble direttamente
(probabilità di precip > soglia calcolata su 6 modelli) piuttosto che il CI
ML post-processato. Il modello ML produce comunque un output per precip, ma
non è il segnale primario per queste decisioni.

**Conseguenza**: Sprint 5 deve esporre sia le previsioni ML (per temp) sia le
probabilità ensemble NWP (per precip) nel JSON di output. Non sono due prodotti
separati — sono due colonne dello stesso oggetto previsione.

---

## D-015 — `build_signals_today`: indicatori DLE per D+0 da osservazioni realtime

**Data**: 2026-05-18

**Contesto**: per il giorno corrente (D+0), il DLE può usare sia le previsioni
ML (lead ~0-6h) sia le osservazioni realtime della stazione SIR/Netatmo degli
ultimi 30-60 minuti.

**Problema**: usare solo le previsioni ML per gli indicatori di oggi introduce
uno scarto rispetto alla realtà osservata. Se piove adesso, il SignalBag ML
potrebbe dare `P(precip > 0.2mm)` = 0.3 (probabilistica), mentre il realtime
dice 1.0 (osservato).

**Decisione**: per D+0 si usa `build_signals_today()` che parte da `build_signals()`
e sovrascrive i segnali osservabili con valori deterministici 0/1:
- Precip/vento/umidità → 0 o 1 da misura realtime
- `T2m_p50` → temperatura corrente osservata
- `P(Tmin < X)` e `Tmin_p10` → restano da ML (la notte non è ancora terminata)

**Conseguenza**: gli indicatori di oggi sul frontend riflettono la situazione
*attuale* (realtime), non la previsione. Questo è il comportamento atteso per
uno strumento operativo. Se il realtime non è disponibile (`current_obs=None`),
si usa il fallback ML puro senza degradazione.

**Limitazione**: il vento realtime è spesso `null` (KI-014), quindi `P(wind > 40kmh)`
resta da NWP ensemble anche con `build_signals_today`.

---

## D-016 — Baseline di confronto per le claim di skill

**Data**: 2026-05-29

**Contesto**: il baseline backtest D+0 (`analysis/baseline_backtest.py`) mostra che il
**multimodello-mean grezzo** è già un baseline forte (MAE tmin ~0.75°C su alcune location
nel 2025): gli errori dei singoli NWP si cancellano. Lo skill +25.6% di Sprint 4 è
misurato contro un baseline NWP che implica MAE ~1.22°C — più debole del multimodello-mean
costruito per-location. Le definizioni differiscono per set modelli (4 vs 6), ground truth
(SIR pesato vs stazione primaria) e periodo (CV multi-anno vs 2025).

**Decisione**: per il case study, ogni claim di skill ("meglio di X") va riferita al
**baseline naive più forte ragionevole** — il multimodello-mean per-location — non al
singolo modello NWP né a un ensemble più debole. Un debias costante mensile non basta a
batterlo (a volte lo peggiora per drift inter-annuale del bias): il valore del modello ML
sta nella correzione **condizionale al regime** (stagione × cielo × vento), non nella
rimozione di un bias medio.

**Conseguenza**: prima di pubblicare numeri di skill, riconciliare il baseline di Sprint 4
con il multimodello-mean per-location e ricomputare lo skill contro di esso. Lo stesso
baseline va usato anche per il confronto esterno (LAMMA) quando `benchmark_forecasts` sarà
popolata. Onestà sul baseline = credibilità del case study.

## D-017 — Convenzione timestamp nel DB: UTC naive ovunque

**Data**: 2026-05-30

**Contesto**: la tabella `observations` mescolava tre convenzioni diverse:
SIR realtime (CET naive, UTC+1 fisso), ARPAT NRT (locale CEST naive), Netatmo (UTC naive — TZ stripped dal driver DuckDB). `NOW()` in DuckDB è UTC, quindi le finestre temporali in `output.py` (`NOW() - INTERVAL 3 HOURS`) confrontavano UTC con naive CET, producendo errori di 1-2h in estate.

**Decisione**: tutte le osservazioni **realtime/hourly** in `observations` sono **UTC naive**.
- SIR: pubblica sempre CET (UTC+1 fisso), convertiamo con `_CET = timezone(timedelta(hours=1))` → `-1h`.
- ARPAT NRT: pubblica ora locale (CEST in estate), convertiamo con `_ITALY_TZ.astimezone(UTC)`.
- Netatmo: già UTC naive (invariato).
- `forecasts`: UTC-aware (invariato — modelli NWP ragionano in UTC).
- **SIR daily e ARPAT daily**: etichette di giorno (mezzanotte naive), non istanti. Non convertite per convenzione. `features.py` le tratta come label di calendario.

**Conseguenze**:
- `strftime` in `output.py` usa `%Y-%m-%dT%H:%M:%SZ` per tutti i campi UTC (`current.ts`, `last_run`, `ts_valid` fallback NWP).
- `coverage_empirical_30d` resta misurata sui `forecasts` grezzi ML, non su valori corretti intraday.
- Nota: SIR realtime storici registrati in CEST (estate) avevano CET naive invece di CEST naive → errore residuo di 1h non recuperabile su quei record. Accettato.

## D-018 — `casa_cercina`: target temperatura in quota + accumulo Netatmo forward-looking

**Data**: 2026-06-02

**Contesto**: `casa_cercina` (Sesto Fiorentino, versante S di Monte Morello, 311m) è la prima location a quota collinare. Tutte le stazioni SIR vicine sono nel catino fiorentino, 200-280m più in basso (ΔQ -200/-280m); l'unica SIR a quota comparabile è Vaiano (TOS11000503, 322m, ΔQ+11m) ma a 13.7km, in valle Bisenzio. Le richieste Open-Meteo (`fetchers.py`) non passano `elevation`: il servizio fa downscaling della temperatura sulla quota reale del punto (DEM 90m), quindi le **feature NWP per Cercina sono già a ~311m**.

**Decisione (target)**: ancorare il termo a **Vaiano** (`termo: [TOS11000503]`), non alle SIR di pianura. Allenare "NWP@311m → SIR@pianura" insegnerebbe al modello a ri-scaldare un forecast già corretto in quota: è un train/serve skew in quota, lo stesso errore vietato per ERA5 (D-001). Vaiano è inoltre una correzione di lapse rate **empirica e inversion-aware** (stazione reale), migliore di un lapse rate teorico fisso che sbaglia proprio sulle inversioni notturne — dove Cercina, a mezza costa in *thermal belt*, è più interessante. Caveat documentato: Vaiano è fondovalle Bisenzio (pooling freddo notturno) vs mezza costa di Cercina → residuo di rappresentatività. Pluvio/anemo/igro restano su vicine di pianura (meno quota-sensibili).

**Decisione (Netatmo)**: Netatmo **non** entra nel training (`features.py` resta `source='sir_toscana'` — ground truth validato, D-005/anti-pattern). Resta però l'unico potenziale dato iperlocale alla quota giusta. Si avvia quindi un **accumulo daily forward-looking** (`netatmo_daily.py`): il realtime Netatmo viene aggregato in righe `granularity='daily'` (tmin/tmax/humidity) sul giorno locale Europe/Rome, senza toccare il modello. Scopo: in Sprint 9+, con storico sufficiente, **stimare l'offset Cercina↔Vaiano** (residuo del proxy) tenendo SIR come backbone. La precipitazione non è aggregata: il realtime salva `rain_1h` (finestra 60min mobile, campionata ~30min) → la somma raddoppia; serve dedup oraria dedicata. `tmax` Netatmo è conservata grezza ma inaffidabile (bias solare sui moduli outdoor): QC schermatura-aware rimandato a Sprint 9+.

**Conseguenze**:
- Netatmo migliora comunque il blocco `current` ("adesso") dell'hero, gratis, via selezione bbox dinamica — nessuna config per Cercina.
- Lo storico Netatmo parte dal deploy: l'analisi offset (D-018 punto 3) non è eseguibile prima di 12-18 mesi di realtime.
