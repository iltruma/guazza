# Guazza — Decisioni architetturali

> Questo file documenta le decisioni tecniche e scientifiche con motivazione.
> Ogni voce ha: contesto → opzioni considerate → scelta → ragionamento → conseguenze.

---

## D-001 — ERA5 mai come predittore di forecast

**Contesto**: ERA5 è una reanalisi ECMWF ad alta risoluzione spaziotemporale,
spesso usata come ground truth in letteratura ML meteo.

**Problema**: ERA5 assimila osservazioni reali → è una stima del vero stato
atmosferico. In produzione si hanno solo forecast NWP (ECMWF, ICON-EU, AROME, ICON-2I)
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
fold. Implementazione: aritmetica diretta sulle date (train_end + 7 giorni =
val_start), senza `TimeSeriesSplit` di sklearn.

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
{"coverage_empirical_30d": {"tmin_ci80": 0.81, "tmin_ci90": 0.88, "tmax_ci80": 0.79, "tmax_ci90": 0.87, "precip_ci80": 0.76, "precip_ci90": 0.84}}
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

**Scelta**: i job sono CLI Python idempotenti invocabili da qualsiasi scheduler
(cron, k8s CronJob, systemd). L'invariante è che l'app resti **orchestrator-agnostic**:
il *target di deploy* è libero (cron in LXC o namespace k8s sono entrambi legittimi),
ma vietato è **accoppiare la logica applicativa** a un orchestratore (Prefect/Dagster/
Airflow/Celery) o esporre l'app come PaaS.

**Riferimento canonico**: `AGENTS.md` §"Stack blindato" (tabella completa) +
§"Anti-pattern" + §"Invariante deploy". Questa voce conserva solo il riassunto.

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

**Conseguenza**: il dataset di training `features_daily` usa 4 modelli NWP
(ecmwf_ifs, icon_eu, arome_france, icon_2i). `icon_d2` rimosso in v0.12.5
(KI-025), `gfs025` rimosso in v0.11.1.
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
(probabilità di precip > soglia calcolata su 4 modelli) piuttosto che il CI
ML post-processato. Il modello ML produce comunque un output per precip, ma
non è il segnale primario per queste decisioni.

**Conseguenza**: Sprint 5 deve esporre sia le previsioni ML (per temp) sia le
probabilità ensemble NWP (per precip) nel JSON di output. Non sono due prodotti
separati — sono due colonne dello stesso oggetto previsione.

---

## D-015 — `build_signals_today`: indicatori DLE per D+0 da osservazioni realtime

**Data**: 2026-05-18 (rivisto 2026-06-27)

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

**Rettifica 2026-06-27**: la correzione intraday Tmin/Tmax sulla **card** del
forecast (blocco `days[0].intraday.tmin_corrected_c` / `tmax_corrected_c`) è
stata rimossa. Causa: la correzione sostituiva la previsione ML con la
lettura realtime più bassa del giorno, che in assenza di letture notturne
portava a valori assurdi (es. Tmin = 36°C di pomeriggio). La correzione
intraday per **Tmin** era attiva solo con copertura notturna delle
osservazioni realtime, condizione non garantita dal job cron attuale.

Conseguenza: card `tmin_c` e `tmax_c` di D+0 = previsione ML pura, identica
al grafico. Realtime continua a essere usato per `current` (hero) e per gli
**indicatori DLE** (`build_signals_today`), dove la logica reattiva è utile.

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

**Riconciliazione (2026-06-05)** — i due numeri non erano in conflitto: misurano l'errore
NWP contro **ground truth diversi**, e il backtest 0.75 non riguarda nemmeno il modello ML.

- **+25.6% (Sprint 4)** = skill del **modello ML** vs NWP-ensemble-mean, entrambi valutati
  contro il **target pesato** multi-stazione (il target di training), walk-forward CV con
  embargo. Ri-eseguito oggi a 4 modelli (`walk_forward_cv`): **tmin +32.5%, tmax +42.8%,
  precip −2.4%** (precip pareggia il NWP, conferma D-014). MAE NWP-mean vs target pesato
  ≈ **1.34°C** tmin / **1.43°C** tmax.
- **~0.75°C (backtest 2025)** = MAE del **NWP grezzo** (non il modello ML) vs la **stazione
  SIR primaria**, debias-only, solo 2025, sulle location migliori. È un *floor-of-skill*
  esplorativo, non uno skill score del modello.

Fattore dominante del divario 1.34↔0.75 = **definizione del ground truth**. NWP-mean tmin
2025 per location: vs primaria 0.75–1.33°C, vs target pesato 0.92–1.58°C. Il blend pesato
diverge dalla singola stazione fino a **2.14°C** (lavoro_madda): il NWP grezzo lo manca di
più perché il blend rappresenta il punto-microclima, non il pluviometro più vicino.
L'aggregazione su giorno UTC (in `features_daily`) vs Europe/Rome (nel backtest) è invece
**trascurabile** (~0.01°C su min/max): ipotesi testata e scartata.

**Decisione finale**: ground truth e baseline del case study = **target pesato** (proxy del
microclima, già scelta di prodotto D-005/D-018). Lo skill si cita come ML vs NWP-ensemble-mean
su quel target, walk-forward + embargo → **+32% tmin / +43% tmax**. Il numero 0.75°C **non è
confrontabile** con lo skill ML e non va citato come "il NWP è già buono": è una metrica
diversa (NWP grezzo vs singolo gauge). Per trasparenza, nel case study si riporta comunque
la MAE NWP-vs-primaria come contesto ("il NWP grezzo non è pessimo al pluviometro, ma manca
il microclima"). Punto chiuso.

**Robustness check + scoperta KI-022 (2026-06-05)** — `analysis/skill_vs_primary.py` valuta
sia NWP sia modello ML contro la **stazione primaria** (gauge fisico indipendente),
out-of-sample con lo stesso split walk-forward. Prima esecuzione: skill ML tmin **−31%**
(modello peggio del NWP grezzo!), trainato fino a un **bug di pipeline** (KI-022):
`obs_weighted` joinava anche su `location_id`, scartando i contributi delle stazioni
condivise → target di training corrotto. `lavoro_cosimo` aveva il target **nullo al 100%**
(mai addestrato); `lavoro_madda` un bias di −2°C.

Dopo fix + rebuild + retrain:
- **Skill vs target pesato (CV canonica)**: tmin +15.6%, tmax +42.6%, precip −2.9%. Il
  +32.5% tmin precedente era gonfiato anche dal target corrotto: il numero onesto è ~+16%.
  tmax era robusto (+43%).
- **Skill vs gauge primario indipendente**: tmin **+8.1%**, tmax **+26.1%**. Modesto ma
  positivo. Star: casa_cesto (+28% tmin), casa_cercina (+49% tmax). Punto debole: casa_nicco
  (negativo) — non un difetto ma il floor del post-processing (vedi sotto).

**Conclusione per il case study**: la claim "meglio degli altri" regge in modo robusto su
**tmax** (+26% vs gauge indipendente, +43% vs target), in modo **modesto** su tmin (+8/+16%),
ed è **nulla** su precip. Va dichiarata così, per location, senza headline unico gonfiato.
Numeri di skill: usare la CV corretta post-KI-022, mai i pre-fix.

**Backtest multi-lead D+0…D+7 (2026-06-05)** — con `ingest multilead` (archivio
`previous_dayN`, disponibile da ~nov 2025) e `analysis/backtest_multilead.py`: modello
addestrato sui dati prima del 2025-10-08 (embargo), valutato out-of-sample sulla finestra
nov 2025→giu 2026, lead per lead. **Guazza batte il NWP-mean a ogni lead.**

- **tmin**: MAE NWP degrada 1.04→2.75°C (D+0→D+7), Guazza 0.81→2.04°C. Skill vs target
  +19…+36%, vs gauge primario +13…+33% — **positivo a ogni lead e crescente** con
  l'orizzonte (a D+5 salva ~0.9°C in assoluto).
- **tmax**: MAE NWP 1.30→2.30°C, Guazza 0.80→1.86°C. Skill vs target +19…+38%; vs gauge
  più marginale a corto lead (+5/+3% a D+1/D+3) ma positivo (+10/+13%) a lead lungo.
- **Lettura**: il valore del post-processing **cresce in assoluto col lead**, perché la
  previsione pubblica peggiora di più dove il microclima conta. La tesi regge su tutto
  l'orizzonte, non solo nel nowcast.

**Caveat**: finestra ~7 mesi (una stagione, inverno-primavera), contigua e singola (non
multi-fold); a lead lungo l'ensemble è solo-ECMWF (orizzonte degli altri modelli più corto).
La versione multi-anno/multi-stagione si accumula solo in avanti dal deploy. Questi numeri
sono un risultato **preliminare ma onesto**, sufficiente per il primo articolo.

**`casa_nicco` negativo — floor del post-processing, non un difetto (2026-06-05)**: dopo
KI-022 casa_nicco resta l'unica location con skill tmin negativo (−8% vs target e vs gauge).
Causa: il bias grezzo del NWP-mean per casa_nicco tmin è **−0.11°C**, cioè ~zero — il NWP è
già quasi non distorto lì, non c'è errore sistematico da correggere e il LightGBM quantile
può solo aggiungere rumore. È il **floor strutturale** del post-processing (si migliora solo
dove c'è bias: casa_cesto +1.12°C → +28%, casa_cercina −1.69°C → +48%). In assoluto il −8%
vale **+0.07°C** di MAE (0.92 vs 0.85), operativamente irrilevante. Su tmax il modello
corregge il bias (−0.85°C → +27% vs target); il −25% vs gauge è solo l'offset target↔primaria
di +0.6°C (stessa caveat, al piccolo). Nessuna correzione: uno shrinkage modello↔NWP-mean
dove il bias è ~0 sarebbe un cambio ad ampio impatto per recuperare 0.07°C. Da riportare nel
case study come onestà sul limite ("dove il NWP è già buono, non miglioriamo").

## D-017 — Convenzione timestamp nel DB: UTC naive ovunque

**Data**: 2026-05-30

**Contesto**: la tabella `observations` mescolava convenzioni diverse:
SIR realtime (CET naive, UTC+1 fisso), fonti esterne con timestamp locale, Netatmo (UTC naive — TZ stripped dal driver DuckDB). `NOW()` in DuckDB è UTC, quindi le finestre temporali in `output.py` (`NOW() - INTERVAL 3 HOURS`) confrontavano UTC con naive CET, producendo errori di 1-2h in estate.

**Decisione**: tutte le osservazioni **realtime/hourly** in `observations` sono **UTC naive**.
- SIR: pubblica sempre CET (UTC+1 fisso), convertiamo con `_CET = timezone(timedelta(hours=1))` → `-1h`.
- Fonti esterne con timestamp locale: convertiamo in UTC con il timezone di riferimento.
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

## D-019 — Adaptive Conformal Inference vs online LightGBM per il drift di calibrazione

**Data**: 2026-06-27

**Contesto**: KI-023 — walk-forward CV 2025-2026 mostra drift di calibrazione CQR:
`coverage_80` = 0.688/0.699 vs target 0.80 su tmin/tmax, scarto di 5-11pp.
Il calibration set statico (364 righe, feb-mag 2026) non è rappresentativo
dei dati di produzione futuri. Due vie correttive possibili.

**Opzioni**:
1. **Online LightGBM**: riaddestrare periodicamente il modello su dati freschi
2. **Adaptive Conformal Inference (ACI)**: Gibbs & Candès 2021 — correggere
   solo la confidenza (α_t adattivo), modello congelato

**Scelta**: opzione 2 (ACI).

**Motivazione**:
- Il drift osservato è di **calibrazione** (la predizione puntuale è decente,
  ma i bound CI sono troppo stretti), non di accuratezza (MAE non degradato
  significativamente). Correggere la confidenza è sufficiente e molto più
  economico di riaddestrare un LightGBM con 4 modelli NWP.
- ACI richiede solo le coppie (prediction, actual) già presenti in
  `predictions.*_obs` — nessun accesso alle feature originali, nessun
  accumulo di training set, nessun costo computazionale.
- Online LightGBM su DuckDB single-writer in un CronJob k8s è fragile:
  richiede lock, accumulo features, retrain periodico. Da valutare solo se
  dopo 30-60gg di ACI la copertura è in target ma il MAE cresce.

**Algoritmo**: `alpha_{t+1} = clip(alpha_t + γ·(α_target − err_t), ε, 1−ε)`
con γ = 0.005, ε = 0.01. Mapping α → larghezza CI: `width_corrected =
width_CQR · (α_target / α_t)`. Dopo il cold start, la copertura long-run
marginale converge a 1−α_target indipendentemente dal distribution shift.

**Cold start N=30**: le prime 30 osservazioni usano CQR statico invariato
(`n_updates < 30` → ACI in bypass). Motivazione: sotto 30 aggiornamenti
la stima di α_t è dominata dal rumore (un singolo errore sposta α del
3-5%); 30 è il punto in cui la varianza campionaria è ≤ 10% di γ.
Equivalente a ~30 giorni di produzione (una observation al giorno per
target, dopo che D+0…D+7 sono backfillati).

**Monitor separato** (`src/guazza/monitor.py`): calcola `coverage_30d` per
(target, lead_bucket) aggregato, indipendente dal predict job. Due motivi:
1. Il predict job aggiorna ACI e genera previsioni — confondere feedback
   (coverage reale) e azione (correzione α_t) in un unico job rende il
   loop non debuggabile.
2. Separazione = il monitor può fallire (`/fail` su Healthchecks) senza
   bloccare la generazione delle previsioni, e viceversa.

**Conseguenze**:
- `aci_state` in DuckDB persiste α_t per (target, lead_bucket) — sopravvive
  ai restart.
- Nessuna modifica al contract JSON: il consumatore vede i bound CI di
  sempre, semplicemente corretti da ACI quando warm.
- Se dopo 30-60gg di operatività la copertura è in target ma il MAE cresce
  → riaprire l'opzione online LightGBM (Sprint 10+).

## D-020 — Skill history: append giornaliero invece di riscrittura full

**Data**: 2026-06-27

**Contesto**: la pagina affidabilità (`affidabilita.html`) mostra solo una
curva MAE per lead, aggregata su tutta la finestra skill. L'utente chiede
"vorrei vedere se i modelli ci hanno preso nel passato" — un grafico che
mostri, per ogni giorno passato, il forecast emesso a D-1 vs l'osservato
a D, per ogni modello. Time series pura, non aggregata.

**Alternative considerate**:
- **A. Riscrittura full di un JSON time series a ogni run**: job che interroga
  DuckDB, ricostruisce tutte le time series, scrive il JSON. Semplice ma
  O(window_size) a ogni run — scala male, complica il backfill incrementale.
- **B. Append giornaliero + dump on-demand**: tabella DuckDB dedicata
  `skill_history_daily` con PK composta, append idempotente di ~21 righe per
  location al giorno. Il `dump` ricostruisce il JSON dalla tabella quando
  serve. **Scelto.**
- **C. Computed on-demand nel frontend**: il frontend interroga DuckDB
  direttamente via API. Scartato: rompe il pattern "frontend statico, nginx
  serve JSON", aggiunge complessità operativa (auth, latency, lock).

**Decisione**: B.

**Conseguenze**:
- Ogni giorno il job `skill_history append` aggiunge 21 × 6 location = ~126
  righe a `skill_history_daily`. Costo: pochi ms.
- Il JSON `skill_history.json` viene rigenerato on-demand dal comando `dump`
  (scrittura atomica). Veloce (DuckDB fa la query su ~21 × N × 6 location
  e la restituisce come lista Python).
- Backfill: il comando `append --days N` itera all'indietro, sfruttando la
  PK per idempotenza. Eseguibile in qualsiasi momento senza rischiare
  duplicati.
- Il frontend ha accesso alla finestra completa (la tabella è la verità) e
  può filtrare lato client per 7gg / 30gg / totale. Niente logica di finestra
  lato backend.

**Limitazione**: la PK include `lead_h` (oggi fisso a 24h) per future
estensioni multi-lead (es. confrontare forecast D+0 vs D+3 nel tempo).
Per ora il JSON espone solo lead 24h.

---

## D-021 — Nowcast temporale 30-60 min via Blitzortung

**Contesto**: il sistema attuale (4 NWP + obs + ML) copre bene forecast
orarie/giornaliere e realtime, ma manca un segnale anticipatorio per
"sta arrivando un temporale nei prossimi 30-60 min". Le celle convettive
si formano su scale che i NWP a 9km non risolvono in tempo.

**Decisione**: Blitzortung (fulmini real-time free) come fonte scelta per
il nowcast "temporale in arrivo". Mantiene l'architettura attuale intatta
per forecast e realtime; Blitzortung aggiunge solo il segnale anticipatorio
mancante (precursore canonico del temporale, 30-60 min prima della cella).

**Alternative scartate e perché**:
- **Parsing tile PNG di RainViewer**: fragile, bandwidth, complessità
- **Tomorrow.io Free Plan**: vendor lock-in, validazione NASA limitata a CONUS,
  incoerente con l'architettura "4 NWP + obs + ML" (sostituirebbe il modello
  proprietario interno). Resta opzione per usi non-core futuri (fallback vento
  o altri parametri opzionali)
- **Heuristic realtime** (∆p Netatmo, salto vento SIR, spike RH): orizzonte
  0-15 min, troppo tardi per "30-60 min"

**Implementazione prevista**:
- API: `https://data.blitzortung.org/Data/Protected/lightning.json` (free, no
  auth per query basse; rate limit ~1 query/5s)
- Strategia: strikes ultimi 30-60 min nel raggio di 50km dalla location; se
  presenti, ETA = distanza del più vicino / velocità tipica (~40 km/h)
- Output JSON: `"storm_approaching": {"eta_min": 25, "intensity": "light"|"moderate"|"heavy"}`
  in `current`
- Indicatore DLE opzionale (stile "panni") con semaforo allerta

**Limitazione accettata**: Blitzortung copre solo temporali con fulmini, non
pioggia generica. Per pioggia nei prossimi 30-60 min senza temporale non c'è
alternativa accettabile libera. Se il caso d'uso si amplia, riaprire la
discussione.

**Stato**: accettata, da implementare (coda `status.md` §D-021).

---

## D-022 — Allerte meteo Protezione Civile via allertameteo.app

**Contesto**: il sistema segnala rischi meteo operativi (panni, motorino,
gelata), ma non le **allerte ufficiali** della Protezione Civile. L'utente
deve consultare un sito esterno per sapere se oggi/domani c'è un'allerta
arancione sul suo comune.

**Decisione**: allertameteo.app (community, free, no key) come fonte scelta
per le allerte meteo ufficiali. Mantiene l'architettura "4 NWP + obs + ML"
intatta; aggiunge solo il segnale "allerta" che non esiste oggi nel prodotto.

**Cosa fornisce**: allerte per oggi e domani su 4 livelli (verde/giallo/
arancione/rosso), per 3 tipologie di rischio: idraulico, temporali,
idrogeologico.

**API**: `https://www.allertameteo.app/api/alert/{codice_istat_comune}` —
JSON, free, no auth, no rate limit. Endpoint metadata: `/api/regioni`,
`/api/province`, `/api/comuni`, `/api/zone`. Storico: `/api/storico/download`.

**Sorgente dati**: i bollettini sono sincronizzati dal repo ufficiale
`pcm-dpc/IT-alert-Hub` e dai Centri Funzionali regionali, qualità alta;
servizio terze parti senza SLA.

**Prerequisito**: codici ISTAT comuni per le 6 location in
`config/locations.yaml` (es. Scandicci 048041, Prato 100005, Firenze 048017,
Sesto Fiorentino 048043 — da verificare).

**Output JSON** (nuovo campo in `current`):
```json
"alert": {
  "today": {"level": "giallo", "risks": {"idraulico": "...", "temporali": "...", "idrogeologico": "..."}},
  "tomorrow": {"level": "arancione", "risks": {...}},
  "source": "allertameteo.app",
  "bulletin_date": "2026-07-29",
  "bulletin_time": "14:32"
}
```

**Indicatore DLE opzionale** (stile "panni"): semaforo 4 colori basato sul
livello massimo fra oggi/domani, con verdict testuale ("Stai in casa" per
arancione+, ecc.).

**Schedule**: 1 fetch ogni 6h è sufficiente (bollettino emesso 1 volta/giorno,
con aggiornamenti durante eventi). Schedulabile in coda alla `pipeline 6h`.

**Rischio accettato**: allertameteo.app è singolo developer, niente SLA.
**Fallback**: DPC repo GitHub
`pcm-dpc/DPC-Bollettini-Criticita-Idrogeologica-Idraulica` (PDF/ZIP ufficiale,
serve parser) — da implementare solo se allertameteo.app sparisce.

**Complementare a D-021**: D-021 (Blitzortung) = nowcast breve fulmini;
D-022 (allertameteo) = allerte ufficiali 24-48h ahead. Insieme coprono
"sta arrivando" + "è previsto".

**Stato**: accettata, da implementare (coda `status.md` §D-022).

---

## D-023 — Fold 1-2 esclusi dalla CV: benchmark eterogeneo, non dati mancanti

**Contesto**: La walk-forward CV a 4 fold mostra fold 1-2 (2023-2024) con
`lead_time_h=0` per tutti i modelli tranne ICON-EU. Il multilead storico
(`previous_dayN`) parte da nov 2022 per ICON-EU, feb 2024 per ECMWF IFS 0.25°,
gen 2024 per AROME France, apr 2025 per ICON-2I.

**Il problema non è tecnico ma metodologico**: fold 1-2 misurano skill vs
ICON-EU singolo (unico modello con multilead pre-2024); fold 3-4 misurano skill
vs consensus ensemble a 4 modelli. L'ensemble a 4 modelli è una baseline più
forte (error averaging) → aggregare i fold produce una metrica non interpretabile
che mischia benchmark eterogenei.

Nota: i dati ICON-EU multilead 2022-2023 **esistono nel DB** e vengono già usati
nel training set (`train_all` usa tutto `features_daily`). Il problema riguarda
solo la *valutazione CV*, non il fitting del modello.

**Opzioni considerate**:
1. Scaricare ECMWF IFS 0.4° (da nov 2022): collineare con 0.25° + distribution
   shift tra cicli ECMWF (47R2→50R1) → scartato
2. Reintrodurre GFS (da mar 2021): rimosso per KI-025 (6.7% null) → scartato
3. ARPEGE Europe come 5° modello storico: collinearità con ECMWF, cascade su 8
   blocchi ensemble in features.py, ensemble ancora diverso da prod → scartato
4. Missingness indicator (`has_ecmwf`, `has_arome`, `has_icon2i`): valido
   concettualmente se il training include pre-2024; marginale se il training
   parte da quando l'ensemble è stabile. Riaprire quando si decide la finestra
   temporale del training definitivo.
5. Script `analysis/` separato "skill vs ICON-EU singolo 2022-2023": opzionale
   per il case study, zero impatto su schema/modello.

**Decisione**: riportare solo fold 3-4 come rappresentativi del sistema in
produzione. Wording canonico per il case study:
> "Walk-forward CV su 24 mesi (ott 2024 – ago 2026) con ensemble multilead
> parziale. Fold precedenti esclusi: archivio `previous_dayN` Open-Meteo
> disponibile solo da 2024 per ECMWF/AROME (ICON-EU da nov 2022), rendendo
> il benchmark non comparabile con il regime operativo a 4 modelli."

**Conseguenze**:
- La claim di skill è conservativa (baseline ensemble più forte dei fold legacy)
- Il sistema continua ad addestrarsi su tutto `features_daily` incluso il pre-2024
- La nota di esclusione è obbligatoria per onestà scientifica nel case study

**Stato**: accettata (sessione 2026-08-04, council multi-modello unanime).
