-- Guazza — DuckDB schema
-- Applicato da duckdb_client.py::init_schema() a primo avvio.
-- Tutte le tabelle usano IF NOT EXISTS: sicuro da rieseguire.

-- ── Configurazione location ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS locations (
    id              VARCHAR PRIMARY KEY,
    label           VARCHAR NOT NULL,
    lat             DOUBLE  NOT NULL,
    lon             DOUBLE  NOT NULL,
    elevation_m     INTEGER,
    sir_station_id  VARCHAR,
    arpat_station_id VARCHAR,
    metadata        JSON
);

-- ── Previsioni grezze multi-modello (NWP) ─────────────────────────────────
-- Una riga per (sorgente, location, run, valid_time, variabile).
-- Partizionamento logico per source + date(ts_run): query analitiche efficienti.
CREATE TABLE IF NOT EXISTS forecasts_raw (
    source          VARCHAR   NOT NULL,   -- 'open_meteo_ecmwf', 'open_meteo_icon_eu', ...
    location_id     VARCHAR   NOT NULL,
    ts_run          TIMESTAMP NOT NULL,   -- data/ora del run del modello
    ts_valid        TIMESTAMP NOT NULL,   -- data/ora valida della previsione
    lead_time_h     INTEGER   NOT NULL,   -- ore di anticipo (ts_valid - ts_run)
    variable        VARCHAR   NOT NULL,   -- 'temperature_2m', 'precipitation', ...
    value           DOUBLE,
    PRIMARY KEY (source, location_id, ts_run, ts_valid, variable)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_raw_location_ts
    ON forecasts_raw (location_id, ts_valid);

-- ── Osservazioni meteo ground truth ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS observations (
    source          VARCHAR   NOT NULL,   -- 'sir_toscana', 'netatmo'
    station_id      VARCHAR   NOT NULL,   -- ID stazione nella sorgente
    location_id     VARCHAR   NOT NULL,   -- FK logica a locations.id
    ts              TIMESTAMP NOT NULL,
    variable        VARCHAR   NOT NULL,   -- 'temperature_2m', 'precipitation', ...
    value           DOUBLE,
    flag            VARCHAR,              -- QC flag sorgente ('ok', 'estimated', 'missing')
    weight          DOUBLE,               -- peso stazione da station_weights (NULL = non calcolato)
    qc_pass         BOOLEAN,              -- NULL per SIR (sempre valide), TRUE/FALSE per Netatmo
    PRIMARY KEY (source, station_id, location_id, ts, variable)
);

CREATE INDEX IF NOT EXISTS idx_observations_location_ts
    ON observations (location_id, ts);

-- ── Idrometria Bisenzio ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hydro_observations (
    station_id      VARCHAR   NOT NULL,
    river           VARCHAR,
    ts              TIMESTAMP NOT NULL,
    level_cm        DOUBLE,
    threshold_1_cm  DOUBLE,   -- soglia allerta 1 (verde/giallo)
    threshold_2_cm  DOUBLE,   -- soglia allerta 2 (giallo/arancio)
    threshold_3_cm  DOUBLE,   -- soglia allerta 3 (arancio/rosso)
    PRIMARY KEY (station_id, ts)
);

-- ── Qualità aria ARPAT ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS air_quality (
    station_id      VARCHAR   NOT NULL,
    location_id     VARCHAR   NOT NULL,
    ts              TIMESTAMP NOT NULL,
    pm10            DOUBLE,   -- µg/m³
    pm25            DOUBLE,   -- µg/m³
    no2             DOUBLE,   -- µg/m³
    o3              DOUBLE,   -- µg/m³
    PRIMARY KEY (station_id, ts)
);

-- ── Predizioni ML quantile ────────────────────────────────────────────────
-- Una riga per (modello, location, run, valid_time, variabile).
-- value_obs: osservazione corrispondente per metriche accuracy (popolato a posteriori).
CREATE TABLE IF NOT EXISTS predictions (
    model_version   VARCHAR   NOT NULL,   -- 'lgbm_quantile_v1.0'
    location_id     VARCHAR   NOT NULL,
    ts_run          TIMESTAMP NOT NULL,
    ts_valid        TIMESTAMP NOT NULL,
    lead_time_h     INTEGER   NOT NULL,
    variable        VARCHAR   NOT NULL,
    value_p10       DOUBLE,
    value_p25       DOUBLE,
    value_p50       DOUBLE,
    value_p75       DOUBLE,
    value_p90       DOUBLE,
    value_obs       DOUBLE,   -- NULL finché l'osservazione non è disponibile
    PRIMARY KEY (model_version, location_id, ts_run, ts_valid, variable)
);

CREATE INDEX IF NOT EXISTS idx_predictions_location_ts
    ON predictions (location_id, ts_valid);

-- ── Benchmark altri provider ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS benchmark_forecasts (
    provider        VARCHAR   NOT NULL,   -- 'yr_no', '3bmeteo', 'meteoam', ...
    location_id     VARCHAR   NOT NULL,
    ts_run          TIMESTAMP NOT NULL,   -- quando è stata emessa la previsione
    ts_valid        TIMESTAMP NOT NULL,
    variable        VARCHAR   NOT NULL,
    value           DOUBLE,
    PRIMARY KEY (provider, location_id, ts_run, ts_valid, variable)
);

-- ── Allerte ufficiali ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    source          VARCHAR   NOT NULL,   -- 'prociv'
    zone_code       VARCHAR   NOT NULL,   -- codice zona allerta (es. 'A', 'B', 'Toscana_A')
    issued_at       TIMESTAMP NOT NULL,
    valid_from      TIMESTAMP,
    valid_to        TIMESTAMP,
    severity        VARCHAR,              -- 'verde', 'giallo', 'arancio', 'rosso'
    phenomena       VARCHAR,              -- 'pioggia', 'vento', 'neve', ...
    description     TEXT,
    raw_url         VARCHAR,
    PRIMARY KEY (source, zone_code, issued_at)
);

-- ── Pesi stazioni osservative ─────────────────────────────────────────────
-- Calcolati da station_weights.refresh_station_weights() — aggiornamento mensile.
-- Decay esponenziale su distanza (half-weight 3km) e delta quota (100m).
CREATE TABLE IF NOT EXISTS station_weights (
    station_id      VARCHAR   NOT NULL,
    source          VARCHAR   NOT NULL,   -- 'sir' | 'netatmo'
    location_id     VARCHAR   NOT NULL,
    weight          DOUBLE    NOT NULL,
    distance_km     DOUBLE,
    delta_elev_m    DOUBLE,
    computed_at     TIMESTAMP DEFAULT now(),
    PRIMARY KEY (station_id, location_id)
);

-- ── Log fetch stazioni Netatmo dinamiche ─────────────────────────────────
-- Ogni fetch getpublicdata produce N righe (una per stazione usata).
-- Permette post-mortem su quali stazioni hanno contribuito e debug offline.
CREATE TABLE IF NOT EXISTS netatmo_fetch_log (
    fetched_at   TIMESTAMP NOT NULL,
    location_id  VARCHAR   NOT NULL,
    station_id   VARCHAR   NOT NULL,   -- MAC address NAMain
    lat          DOUBLE    NOT NULL,
    lon          DOUBLE    NOT NULL,
    alt_m        INTEGER,
    distance_km  DOUBLE    NOT NULL,
    delta_elev_m DOUBLE    NOT NULL,
    weight       DOUBLE    NOT NULL,
    temperature  DOUBLE,               -- °C (null se dato non disponibile)
    humidity     DOUBLE,               -- % (null se dato non disponibile)
    rain_1h      DOUBLE,               -- mm/h (null se dato non disponibile)
    wind_speed   DOUBLE,               -- m/s (null se dato non disponibile)
    PRIMARY KEY (fetched_at, location_id, station_id)
);

-- ── Log Decision Logic Engine ─────────────────────────────────────────────
-- Ogni invocazione del DLE produce un record qui. Essenziale per post-mortem
-- e per il materiale dell'articolo (quante volte il modello ha sbagliato indicatore).
CREATE TABLE IF NOT EXISTS indicator_log (
    ts              TIMESTAMP NOT NULL,
    location_id     VARCHAR   NOT NULL,
    indicator_id    VARCHAR   NOT NULL,   -- 'panni', 'motorino', 'gelata', ...
    input_summary   JSON,                 -- distribuzione input usata per la decisione
    rule_matched    VARCHAR,              -- testo della regola che ha fatto match
    verdict         VARCHAR   NOT NULL,   -- 'verde', 'giallo', 'rosso'
    probability     DOUBLE,              -- confidenza associata al verdict
    alpha           DOUBLE,              -- soglia ottima = cost_fp / (cost_fp + cost_fn)
    cost_fn         DOUBLE,
    cost_fp         DOUBLE,
    PRIMARY KEY (ts, location_id, indicator_id)
);
