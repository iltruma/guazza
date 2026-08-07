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

**Scelta**: calibration set separato per 6 bucket di lead time **giornalieri**
(il multilead di `features_daily` è in multipli di 24h; bucket orari produrrebbero
strati sempre vuoti con fallback sistematico al bucket adiacente):
- `D+0`, `D+1`, `D+2`, `D+3`, `D+4`, `D+5+` (match `LEAD_BUCKETS` in `models.py`)

Un set di residui (e quindi di quantili conformali) per bucket.

**Conseguenze**: 6× la dimensione del calibration set necessario; minimo
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

**Stato (2026-08-07) — due canali complementari**: il campo è ancora emesso
per location (`compute_coverage_30d` → JSON location), ma la fonte primaria
di onestà scientifica in UI è `skill.json` → `locations.*.coverage`
(CI80/CI90 empirici per lead D+0..D+7, intervalli CQR+ACI di produzione, card
"Copertura intervalli" in affidabilita.html). Il 30d aggregato nel JSON location
resta come monitor leggero; non è letto dal frontend principale.

---

## D-005 — Modello globale con location-id categorica

**Contesto**: 6 location, dati storici limitati per location.

**Opzioni**:
1. Modelli indipendenti per location (uno per ciascun modello NWP — 4 oggi)
2. 1 modello globale con `location_id` come feature categorica

**Scelta**: opzione 2.

**Motivazione**: dati storici per-location sono pochi (anni, non decenni).
Il modello globale trasferisce informazione tra location. LightGBM gestisce
natively le categoriche. Se una location ha dati migliori, contribuisce di
più senza danneggiare le altre.

**Conseguenza**: serve `location_id` come categoria in ogni riga del training
set. Niente one-hot encoding. Unica eccezione al training su osservazioni SIR
validated: D-024 (correttore orario) usa realtime NRT solo per la forma del
profilo, con QC dedicato.

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

## D-013 — Selezione modelli NWP per post-processing iper-locale

**Data**: 2026-05-16 (snellita 2026-08-07)

**Contesto**: la risoluzione spaziale dei modelli globali (25 km) è
insufficiente per catturare i microclimi toscani complessi (es. inversione
termica nella piana di Campi o Scandicci).

**Decisione — set finale a 4 modelli** (verificato in `features.py`,
`NWP_MODEL_PREFIXES`):
- `ecmwf_ifs` (HRES, 9 km): sostituisce `ecmwf_ifs025` (25 km) — stessa fisica, orografia molto più dettagliata.
- `icon_eu` (7 km) e `arome_france` (2.5 km): alta risoluzione DWD / Météo-France.
- `italia_meteo_arpae_icon_2i` (2.2 km, ItaliaMeteo/ARPAE): unico che assimila osservazioni italiane, orizzonte 72h.

Scartati: `ecmwf_aifs025` (Open-Meteo restituisce null, KI-011), `gfs025`
(rimosso v0.11.1), `icon_d2` (rimosso v0.12.5, KI-025).

**Conseguenza**: il dataset di training `features_daily` usa i 4 modelli sopra;
necessario rieseguire il backfill `historical` per i nuovi modelli.

---

## D-014 — Precipitazione: rain_clf per la presenza, ceiling dichiarato per l'intensità

**Data**: 2026-05-17 (rivista 2026-08-07)

**Contesto**: walk-forward CV mostra skill MAE precipitazione ≈ 0 — ceiling
strutturale: ground truth SIR rumoroso per la variabilità spaziale degli eventi,
distribuzione zero-inflated, archivio storico lead=0-only. Il post-processing ML
non batte il NWP sull'intensità in mm.

**Decisione — stato attuale**:
1. **Presenza pioggia** → segnale primario = `rain_clf` (hurdle stadio 1,
   BSS +0.16/+0.28, AUC 0.73-0.79): `P(precip > 0.2mm)` nel DLE e in UI
   (`build_signals` in `output.py`), `rain_prob` persistita in `predictions`.
   Niente ensemble-NWP nel DLE per la presenza pioggia. Naming: `rain_clf` è
   l'artifact interno (dict `pred`); nel JSON di output il campo è
   `precip_mm.prob_rain` (contract.md), la UI legge `rain_prob`.
2. **Intensità mm** → ceiling dichiarato: i quantili ML restano in output solo
   come display/CI, senza claim di skill sull'intensità; `precip_mm.mean` resta
   il valore atteso E[precip] per valutazione economica/rischio (contract.md),
   non una predizione puntuale da citare come skill. Wet regressor rimosso
   (2026-08-04, skill negativo in 3/4 fold).
3. **Nessun modello orario precip** (conferma D-024): `precip_prob_ml` oraria =
   prob daily ML distribuita sul timing NWP — non un modello orario indipendente,
   non prob oraria calibrata.

L'ensemble NWP resta il segnale per vento/umidità/nebbia.

**Conseguenza**: il JSON espone sia i quantili ML (intensità) sia la P(pioggia)
ML (`rain_prob`, Brier per lead in `skill.json`) — non due prodotti separati,
due colonne dello stesso oggetto previsione.

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

**Data**: 2026-05-29 (snellita 2026-08-07 — narrativa storica: git history)

**Decisione**: per il case study ogni claim di skill ("meglio di X") va riferita al
**baseline naive più forte ragionevole** — il multimodello-mean per-location — non al
singolo modello NWP né a un ensemble più debole. Ground truth = **target pesato**
(proxy del microclima, D-005/D-018); valutazione walk-forward CV con embargo 7gg
(D-002). Il valore del modello ML sta nella correzione **condizionale al regime**
(stagione × cielo × vento), non nella rimozione di un bias medio: un debias costante
non basta a batterlo (a volte lo peggiora per drift inter-annuale del bias).

**Numeri canonici (fold 3-4, unici rappresentativi — D-023)**, skill ML vs
NWP-ensemble-mean su target pesato:
- **tmax +30-32%** — robusto, model-agnostic; obiettivo raggiunto.
- **tmin +7-25%** — positivo ma variabile per location; casa_nicco (bias NWP ≈ 0) è
  il **floor strutturale** del post-processing: dove il NWP è già non distorto non si
  migliora (+0.07°C MAE in assoluto, operativamente irrilevante).
- **precip +3-11% ≈ 0** — ceiling strutturale (D-014), nessuna claim di skill.
- **rain_clf BSS +0.16/+0.28** — P(pioggia) funziona.

**Caveat metodologico (fold 1-2 esclusi)**: i fold 1-2 (pre-ott 2024) misurano lo
skill contro ICON-EU singolo (unico modello con archivio multilead pre-2024); i fold
3-4 contro l'ensemble a 4 modelli, baseline più forte — benchmark eterogenei non
aggregabili in una metrica unica (D-023).

**Skill vs gauge primario indipendente** (post-KI-022, `analysis/skill_vs_primary.py`):
tmin **+8%**, tmax **+26%** — il target pesato è un proxy del microclima, il gauge è
la verità osservata; backtest multilead D+0..D+7: tmin +13…+33%, tmax +5…+13% vs gauge.
Nel case study si riportano entrambi (vs target e vs gauge), per location.

Dettaglio per-location (KI-022, backtest multilead D+0..D+7): materiale case study —
storico completo in git.

**Conseguenza**: lo stesso baseline va usato per il confronto esterno (LAMMA) quando
`benchmark_forecasts` sarà popolata. Onestà sul baseline = credibilità del case study.

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

**Contesto**: KI-023 — walk-forward CV 2025-2026: drift di calibrazione CQR,
`coverage_80` = 0.688/0.699 vs target 0.80 (tmin/tmax); il calibration set statico
non è rappresentativo dei dati di produzione futuri.

**Opzioni**: (1) online LightGBM — riaddestramento periodico su dati freschi;
(2) ACI (Gibbs & Candès 2021) — correggere solo la confidenza (α_t adattivo),
modello congelato.

**Scelta**: opzione 2 (ACI).

**Motivazione**: il drift è di **calibrazione**, non di accuratezza (MAE non
degradato) → correggere la confidenza è sufficiente e molto più economico.
ACI richiede solo le coppie (prediction, actual) già in `predictions.*_obs` —
nessun accumulo di training set. Online LightGBM su DuckDB single-writer in un
CronJob k8s è fragile (lock, retrain periodico); da valutare solo se dopo 30-60gg
di ACI la copertura è in target ma il MAE cresce.

**Algoritmo**: `alpha_{t+1} = clip(alpha_t + γ·(α_target − err_t), ε, 1−ε)`
con γ = 0.005, ε = 0.01. Mapping α → larghezza CI: `width_corrected =
width_CQR · (α_target / α_t)`. Coverage long-run marginale converge a 1−α_target.

**Cold start N=30**: le prime 30 osservazioni usano CQR statico invariato
(`n_updates < 30` → ACI in bypass): sotto 30 aggiornamenti α_t è dominato dal
rumore (un errore sposta α del 3-5%); 30 è il punto in cui la varianza
campionaria è ≤ 10% di γ. ~30 giorni di produzione.

**Monitor separato** (`src/guazza/monitor.py`): `coverage_30d` per (target,
lead_bucket) in job indipendente dal predict. Motivo: feedback (coverage reale)
e azione (correzione α_t) in un unico job rendono il loop non debuggabile; il
monitor può fallire (push `status=down` su Uptime Kuma, `KUMA_PUSH_URL_MONITOR`)
senza bloccare le previsioni e viceversa.

**Conseguenze**:
- Implementazione: `src/guazza/aci.py` (`ACI_COLD_START_N=30`); stato `aci_state`
  in DuckDB persiste α_t per (target, lead_bucket) — sopravvive ai restart.
- Nessuna modifica al contract JSON: il consumatore vede i bound CI di sempre,
  corretti da ACI quando warm.
- Se dopo 30-60gg di operatività la copertura è in target ma il MAE cresce
  → riaprire l'opzione online LightGBM (Sprint 10+).

## D-020 — Skill history: append giornaliero invece di riscrittura full

**Data**: 2026-06-27

**Contesto**: la pagina affidabilità mostrava solo una curva MAE aggregata;
serviva una time series giorno-per-giorno (forecast D-1 vs osservato D, per modello).

**Alternative**:
- **A. Riscrittura full JSON a ogni run**: O(window_size), complica il backfill.
- **B. Append giornaliero + dump on-demand**: tabella `skill_history_daily` con PK
  composta, append idempotente di ~21 righe/location/giorno; il `dump` ricostruisce
  il JSON dalla tabella. **Scelto.**
- **C. Computed on-demand nel frontend**: rompe "frontend statico, nginx serve
  JSON" (auth, latency, lock). Scartato.

**Conseguenze**:
- Il job `skill_history append` aggiunge ~126 righe/giorno (21 × 6 location);
  il JSON `skill_history.json` è rigenerato on-demand dal comando `dump`
  (scrittura atomica). Backfill: `append --days N` iterativo all'indietro,
  idempotente per PK.
- Il frontend ha accesso alla finestra completa (la tabella è la verità) e
  filtra lato client. Niente logica di finestra lato backend.
- **Limitazione**: la PK include `lead_h` (oggi fisso 24h) per future estensioni
  multi-lead; per ora il JSON espone solo lead 24h.

**Confini con `skill.json` (2026-08-06)**: `skill_history_daily` = time series
giorno-per-giorno (MAE per modello nel tempo, lead fisso 24h); `skill.json` =
aggregati per lead (MAE, coverage CI, Brier). La sessione 2026-08-06 ha cambiato
la fonte della curva skill (predictions di produzione), non questo canale.

---

## D-021 — Nowcast temporale 30-60 min via Blitzortung

**Contesto**: i NWP a 9km non risolvono le celle convettive in tempo; manca un
segnale anticipatorio per "temporale in arrivo nei prossimi 30-60 min".

**Decisione**: Blitzortung (fulmini real-time free) come fonte per il nowcast
"temporale in arrivo". Aggiunge solo il segnale anticipatorio mancante, senza
toccare l'architettura forecast/realtime.

**Alternative scartate**: parsing tile PNG RainViewer (fragile, bandwidth);
Tomorrow.io Free Plan (vendor lock-in, incoerente con "4 NWP + obs + ML");
heuristic realtime ∆p/salto vento/spike RH (orizzonte 0-15 min, troppo corto).

**Implementazione prevista**:
- API: `https://data.blitzortung.org/Data/Protected/lightning.json` (free, no
  auth per query basse; rate limit ~1 query/5s)
- Strategia: strikes ultimi 30-60 min nel raggio di 50km dalla location; ETA =
  distanza del più vicino / velocità tipica (~40 km/h)
- Output JSON: `"storm_approaching": {"eta_min": 25, "intensity": "light"|"moderate"|"heavy"}`
  in `current`
- Indicatore DLE opzionale (stile "panni") con semaforo allerta

**Limitazione accettata**: Blitzortung copre solo temporali con fulmini, non
pioggia generica senza temporale. Se il caso d'uso si amplia, riaprire.

**Stato**: accettata, da implementare (coda `status.md` §D-021).

---

## D-022 — Allerte meteo Protezione Civile via allertameteo.app

**Contesto**: il sistema segnala rischi meteo operativi (panni, motorino,
gelata), ma non le **allerte ufficiali** della Protezione Civile (oggi/domani
sul comune dell'utente).

**Decisione**: allertameteo.app (community, free, no key) come fonte per le
allerte meteo ufficiali. Aggiunge solo il segnale "allerta" che non esiste
oggi nel prodotto.

**Cosa fornisce**: allerte per oggi e domani su 4 livelli (verde/giallo/
arancione/rosso), per 3 tipologie di rischio: idraulico, temporali,
idrogeologico.

**API**: `https://www.allertameteo.app/api/alert/{codice_istat_comune}` —
JSON, free, no auth, no rate limit. Endpoint metadata: `/api/regioni`,
`/api/province`, `/api/comuni`, `/api/zone`. Storico: `/api/storico/download`.

**Sorgente dati**: bollettini sincronizzati dal repo ufficiale
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

**Indicatore DLE opzionale**: semaforo 4 colori sul livello massimo
oggi/domani, con verdict testuale.

**Schedule**: 1 fetch ogni 6h (bollettino 1×/giorno, aggiornamenti durante
eventi), in coda alla pipeline 6h.

**Rischio accettato**: singolo developer, niente SLA. **Fallback**: repo DPC
`pcm-dpc/DPC-Bollettini-Criticita-Idrogeologica-Idraulica` (PDF/ZIP ufficiale,
serve parser) — solo se allertameteo.app sparisce.

**Complementare a D-021**: D-021 (Blitzortung) = nowcast breve fulmini;
D-022 = allerte ufficiali 24-48h ahead.

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

## D-024 — Correttore orario: correzione di forma del profilo `hourly[]`

**Contesto**: il profilo orario daily è la shape NWP ensemble-mean rescalata sugli
anchor ML daily (`compute_hourly_profile`). Gli errori di forma (fase/ampiezza del
ciclo diurno) sono sistematici per location; il modello daily non li vede perché
non osserva mai dati orari.

**Decisione**: correttore di forma opzionale (LightGBM regression p50) addestrato sul
residuo di shape `Δ(h) = obs_median(h) − shape_obs(h)` (shape normalizzata 0..1,
day-invariant; i bias additivi restano responsabilità dei daily anchor ML). Feature:
hour, month, location_id, shape_norm, weather_code modale, flag precip, vento,
umidità. Split cronologico con embargo 7gg; accettazione solo se improvement RMSE
≥ 15% su holdout, altrimenti nessun file (fallback = profilo attuale). In inferenza:
Δ applicato alla curva ancorata ML + ri-ancoraggio a [tmin_p50, tmax_p50] e bande
CI80 ai rispettivi bound → livelli sempre ML daily, cambia solo la forma; contract
JSON invariato.

**Eccezione scoped a D-005**: il training usa osservazioni realtime NRT non validate
(SIR realtime + Netatmo) come target di un bias di forma, non come valori di record.
Mitigazioni obbligatorie: nuovi flag QC (`spike_realtime`, `stall_sensor`,
`bias_solar`) + aggregazione mediana per slot con minimo campioni (3). D-005 resta
in vigore per i target daily e le feature del modello principale.

**Esclusioni**: precip orario (D-014: ceiling intensità, nessun target orario),
umidità; nessun impatto su CV/CQR/ACI.

**Conseguenze**: dati sufficienti per l'allenamento arrivano dall'accumulo realtime
in prod (~60 giorni/location); prima il correttore non è addestrato (cold-start
silenzioso). La skill daily non si muove: il guadagno è l'onestà della curva.

**Stato**: accettata (sessione 2026-08-07).

---

## Decisioni rimosse

Rimosse dalla revisione 2026-08-07 (testo completo nel git history):

- **D-007** — Stack blindato: contenuto canonico in `AGENTS.md` §"Stack blindato"
  (tabella completa + anti-pattern + invariante deploy).
- **D-011** — Coordinate batching Open-Meteo: implementazione in `fetch_openmeteo.py`.
- **D-012** — Temporal chunking backfill: vincolo operativo in KI-004 +
  `_OM_CELL_BUDGET` in `fetch_openmeteo.py`.
