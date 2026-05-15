-- Guazza — DuckDB schema (wide)
-- Una riga per (source, station_id, ts). Colonne sparse per variabili.
-- DuckDB gestisce NULL in columnar mode senza overhead.

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

-- ── Osservazioni meteo ground truth ───────────────────────────────────────────
-- Wide: una riga per (source, station_id, ts).
-- Ogni stazione produce una sola riga per timestamp.
CREATE TABLE IF NOT EXISTS observations (
    source       VARCHAR   NOT NULL,
    station_id   VARCHAR   NOT NULL,
    location_id  VARCHAR   NOT NULL,
    ts           TIMESTAMP NOT NULL,
    temp_c          DOUBLE,
    tmin_c          DOUBLE,
    tmax_c          DOUBLE,
    humidity_pct    DOUBLE,
    precip_mm       DOUBLE,
    precip_interval_h TINYINT, -- granularità: 1=1h, 24=24h. NULL se ignota.
    wind_speed_ms   DOUBLE,
    wind_dir_deg    DOUBLE,
    wind_gust_ms    DOUBLE,
    pressure_hpa    DOUBLE,
    level_m         DOUBLE,
    pm10_ugm3       DOUBLE,
    pm25_ugm3       DOUBLE,
    no2_ugm3        DOUBLE,
    o3_ugm3         DOUBLE,
    weight          DOUBLE,
    qc_pass         BOOLEAN,
    PRIMARY KEY (source, station_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_observations_location_ts
    ON observations (location_id, ts);

-- ── Previsioni grezze multi-modello (NWP) ────────────────────────────────────
-- Wide: una riga per (source, location_id, ts_run, ts_valid).
CREATE TABLE IF NOT EXISTS forecasts (
    source       VARCHAR   NOT NULL,
    location_id  VARCHAR   NOT NULL,
    ts_run       TIMESTAMP NOT NULL,
    ts_valid     TIMESTAMP NOT NULL,
    lead_time_h  INTEGER   NOT NULL,
    temp_c          DOUBLE,
    humidity_pct    DOUBLE,
    precip_mm       DOUBLE,
    wind_speed_ms   DOUBLE,
    wind_dir_deg    DOUBLE,
    wind_gust_ms    DOUBLE,
    pressure_hpa    DOUBLE,
    PRIMARY KEY (source, location_id, ts_run, ts_valid)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_location_ts
    ON forecasts (location_id, ts_valid);

-- ── Predizioni ML quantile ────────────────────────────────────────────────────
-- Wide: una riga per (model_version, location_id, ts_valid, lead_time_h).
-- Variabili predette come colonne quantile (es. temp_p10, temp_p50, temp_p90).
-- Altre variabili aggiunte con ALTER TABLE se necessario.
CREATE TABLE IF NOT EXISTS predictions (
    model_version VARCHAR   NOT NULL,
    location_id   VARCHAR   NOT NULL,
    ts_valid      TIMESTAMP NOT NULL,
    lead_time_h   INTEGER   NOT NULL,
    temp_p10      DOUBLE,
    temp_p50      DOUBLE,
    temp_p90      DOUBLE,
    temp_obs      DOUBLE,
    PRIMARY KEY (model_version, location_id, ts_valid, lead_time_h)
);

CREATE INDEX IF NOT EXISTS idx_predictions_location_ts
    ON predictions (location_id, ts_valid);

-- ── Benchmark altri provider ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS benchmark_forecasts (
    provider    VARCHAR   NOT NULL,
    location_id VARCHAR   NOT NULL,
    ts_run      TIMESTAMP NOT NULL,
    ts_valid    TIMESTAMP NOT NULL,
    temp_c          DOUBLE,
    humidity_pct    DOUBLE,
    precip_mm       DOUBLE,
    wind_speed_ms   DOUBLE,
    PRIMARY KEY (provider, location_id, ts_run, ts_valid)
);

-- ── Allerte ufficiali ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    source     VARCHAR   NOT NULL,
    zone_code  VARCHAR   NOT NULL,
    issued_at  TIMESTAMP NOT NULL,
    valid_from TIMESTAMP,
    valid_to   TIMESTAMP,
    severity   VARCHAR,
    phenomena  VARCHAR,
    description TEXT,
    raw_url    VARCHAR,
    PRIMARY KEY (source, zone_code, issued_at)
);

-- ── Pesi stazioni osservative ─────────────────────────────────────────────
-- Calcolati da weights.refresh_station_weights() — aggiornamento mensile.
CREATE TABLE IF NOT EXISTS station_weights (
    station_id   VARCHAR   NOT NULL,
    source       VARCHAR   NOT NULL,
    location_id  VARCHAR   NOT NULL,
    weight       DOUBLE    NOT NULL,
    distance_km  DOUBLE,
    delta_elev_m DOUBLE,
    computed_at  TIMESTAMP DEFAULT now(),
    PRIMARY KEY (station_id, location_id)
);

-- ── Log fetch stazioni Netatmo dinamiche ────────────────────────────────────
CREATE TABLE IF NOT EXISTS netatmo_fetch_log (
    fetched_at   TIMESTAMP NOT NULL,
    location_id  VARCHAR   NOT NULL,
    station_id   VARCHAR   NOT NULL,
    lat          DOUBLE    NOT NULL,
    lon          DOUBLE    NOT NULL,
    alt_m        INTEGER,
    distance_km  DOUBLE    NOT NULL,
    delta_elev_m DOUBLE    NOT NULL,
    weight       DOUBLE    NOT NULL,
    temperature  DOUBLE,
    humidity     DOUBLE,
    rain_1h      DOUBLE,
    wind_speed   DOUBLE,
    PRIMARY KEY (fetched_at, location_id, station_id)
);

-- ── Log Decision Logic Engine ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS indicator_log (
    ts           TIMESTAMP NOT NULL,
    location_id  VARCHAR   NOT NULL,
    indicator_id VARCHAR   NOT NULL,
    input_summary JSON,
    rule_matched VARCHAR,
    verdict      VARCHAR   NOT NULL,
    probability  DOUBLE,
    alpha        DOUBLE,
    cost_fn      DOUBLE,
    cost_fp      DOUBLE,
    PRIMARY KEY (ts, location_id, indicator_id)
);
