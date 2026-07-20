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
-- Wide: una riga per (source, station_id, ts, granularity).
-- granularity: 'daily' (CSV SIR, aggregato giornaliero),
--              'realtime' (SIR actions.php, Netatmo — lettura istantanea),
--              'hourly' (future sorgenti orarie)
-- La granularity è in PK perché lo stesso (source, station_id, ts=00:00)
-- può esistere sia come daily (CSV) sia come realtime (chiamata a mezzanotte).
CREATE TABLE IF NOT EXISTS observations (
    source       VARCHAR   NOT NULL,
    station_id   VARCHAR   NOT NULL,
    location_id  VARCHAR   NOT NULL,
    ts           TIMESTAMP NOT NULL,
    granularity  VARCHAR   NOT NULL, -- 'daily' | 'realtime' | 'hourly'
    temp_c          DOUBLE,
    tmin_c          DOUBLE,
    tmax_c          DOUBLE,
    humidity_pct    DOUBLE,
    precip_mm       DOUBLE,
    precip_interval_h TINYINT, -- 1=1h, 24=24h. NULL se ignota.
    precip_cumday_mm  DOUBLE,  -- CUM24 SIR: cumulativo dalla mezzanotte (realtime only)
    wind_speed_ms   DOUBLE,
    wind_dir_deg    DOUBLE,
    wind_gust_ms    DOUBLE,
    pressure_hpa    DOUBLE,
    level_m         DOUBLE,
    pm10_ugm3       DOUBLE,
    pm25_ugm3       DOUBLE,
    no2_ugm3        DOUBLE,
    o3_ugm3         DOUBLE,
    co_mgm3         DOUBLE,   -- CO in mg/m³ (unità diversa dagli altri)
    benzene_ugm3    DOUBLE,
    so2_ugm3        DOUBLE,
    weight          DOUBLE,
    qc_pass         BOOLEAN,
    last_modified   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (source, station_id, ts, granularity)
);

CREATE INDEX IF NOT EXISTS idx_observations_location_ts
    ON observations (location_id, ts, granularity);

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
    weather_code    INTEGER,
    last_modified   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (source, location_id, ts_run, ts_valid)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_location_ts
    ON forecasts (location_id, ts_valid);

-- ── Predizioni ML quantile ────────────────────────────────────────────────────
-- Wide: una riga per (model_version, location_id, ts_valid, lead_time_h).
-- Colonne *_obs popolate dal job predict (backfill via backfill_prediction_obs).
-- Schema v0.5: 3 target × (p05/p10/p50/p90/p95 + ci80_lo/hi + ci90_lo/hi + obs).
CREATE TABLE IF NOT EXISTS predictions (
    model_version VARCHAR   NOT NULL,
    location_id   VARCHAR   NOT NULL,
    ts_valid      TIMESTAMP NOT NULL,
    lead_time_h   INTEGER   NOT NULL,
    tmin_p05     DOUBLE, tmin_p10     DOUBLE, tmin_p50     DOUBLE,
    tmin_p90     DOUBLE, tmin_p95     DOUBLE,
    tmin_ci80_lo DOUBLE, tmin_ci80_hi DOUBLE,
    tmin_ci90_lo DOUBLE, tmin_ci90_hi DOUBLE,
    tmin_obs     DOUBLE,
    tmax_p05     DOUBLE, tmax_p10     DOUBLE, tmax_p50     DOUBLE,
    tmax_p90     DOUBLE, tmax_p95     DOUBLE,
    tmax_ci80_lo DOUBLE, tmax_ci80_hi DOUBLE,
    tmax_ci90_lo DOUBLE, tmax_ci90_hi DOUBLE,
    tmax_obs     DOUBLE,
    precip_p05     DOUBLE, precip_p10     DOUBLE, precip_p50     DOUBLE,
    precip_p90     DOUBLE, precip_p95     DOUBLE,
    precip_ci80_lo DOUBLE, precip_ci80_hi DOUBLE,
    precip_ci90_lo DOUBLE, precip_ci90_hi DOUBLE,
    precip_obs     DOUBLE,
    generated_at  TIMESTAMP DEFAULT current_timestamp,
    last_modified TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (model_version, location_id, ts_valid, lead_time_h)
);

CREATE INDEX IF NOT EXISTS idx_predictions_location_ts
    ON predictions (location_id, ts_valid);

-- ── Benchmark NWP giornalieri ────────────────────────────────────────────────
-- Aggregati daily per (source, location, data) con obs backfillate dal job predict.
-- Permette confronto sistematico NWP grezzo vs ML nel tempo (skill score evolution).
CREATE TABLE IF NOT EXISTS benchmark_forecasts (
    source       VARCHAR NOT NULL,
    location_id  VARCHAR NOT NULL,
    target_date  DATE    NOT NULL,
    lead_time_h  INTEGER,
    tmin_c       DOUBLE,
    tmax_c       DOUBLE,
    precip_mm    DOUBLE,
    tmin_obs     DOUBLE,
    tmax_obs     DOUBLE,
    precip_obs   DOUBLE,
    last_modified TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (source, location_id, target_date)
);

-- ── Skill history: append giornaliero forecast vs actual per modello ─────────
-- Una riga = (location, target_date, source, variable, lead_h). Popolata dal job
-- `guazza.skill_history append` (cron giornaliero, idempotente grazie alla PK).
-- La vista `skill_history_daily_aggregated` espone serie allineate per il
-- frontend (`affidabilita.html`): per ogni (location, source, variable) tutte le
-- date con actual, in modo da poter filtrare per finestra (7gg / 30gg / totale).
CREATE TABLE IF NOT EXISTS skill_history_daily (
    location_id    VARCHAR  NOT NULL,
    target_date    DATE     NOT NULL,
    source         VARCHAR  NOT NULL,    -- 'guazza' o uno dei 5 NWP
    variable       VARCHAR  NOT NULL,    -- 'tmin_c', 'tmax_c', 'precip_mm'
    lead_h         SMALLINT NOT NULL,    -- 24 per ora (D-1 → D)
    forecast_value DOUBLE,
    actual_value   DOUBLE,
    abs_error      DOUBLE,
    generated_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (location_id, target_date, source, variable, lead_h)
);
CREATE INDEX IF NOT EXISTS skill_history_loc_var_date
    ON skill_history_daily (location_id, variable, target_date);

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
    raw_url       VARCHAR,
    last_modified TIMESTAMP DEFAULT current_timestamp,
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

-- ── Flag qualità dati osservativi ────────────────────────────────────────
-- Popolata da qc.compute_quality_flags() — ricalcolo idempotente full-replace.
-- flag_type: 'spike_tmin' | 'spike_tmax' | 'inversion_temp' | 'range_precip_high'
-- value: valore osservato che ha triggerato il flag (o delta per spike)
CREATE TABLE IF NOT EXISTS quality_flags (
    source       VARCHAR   NOT NULL,
    station_id   VARCHAR   NOT NULL,
    ts           TIMESTAMP NOT NULL,
    granularity  VARCHAR   NOT NULL,
    flag_type    VARCHAR   NOT NULL,
    column_name  VARCHAR   NOT NULL,
    value        DOUBLE,
    detail        VARCHAR,
    last_modified TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (source, station_id, ts, granularity, flag_type, column_name)
);

-- ── Ring pluviometrici upstream ───────────────────────────────────────────
-- Popolata da weights.refresh_upstream_rings() insieme a station_weights.
-- ring_label: 'ring1' (0-20km) | 'ring2' (20-50km) | 'ring3' (50-100km)
CREATE TABLE IF NOT EXISTS upstream_ring_station (
    station_id  VARCHAR NOT NULL,
    location_id VARCHAR NOT NULL,
    ring_label  VARCHAR NOT NULL,
    distance_km DOUBLE,
    PRIMARY KEY (station_id, location_id)
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
    cost_fp       DOUBLE,
    last_modified TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (ts, location_id, indicator_id)
);

-- ── Ground truth: osservazioni SIR daily pesate per location ──────────────────
-- Fonte unica della media pesata stazione→location (decay distanza/quota in
-- station_weights). Usata sia come feature/target (features.py) sia per backfillare
-- i *_obs di predictions e benchmark_forecasts: un'unica definizione evita il
-- train/serve skew che KI-022 ha causato quando la logica divergeva tra i due usi.
-- JOIN solo su station_id (non o.location_id): una stazione condivisa contribuisce
-- a tutte le location che la pesano; la PK di observations non include location_id.
CREATE OR REPLACE VIEW obs_weighted_daily AS
SELECT
    sw.location_id,
    o.ts::DATE AS obs_date,
    SUM(o.tmin_c * sw.weight)
        / NULLIF(SUM(CASE WHEN o.tmin_c IS NOT NULL THEN sw.weight ELSE 0 END), 0)
        AS tmin_c,
    SUM(o.tmax_c * sw.weight)
        / NULLIF(SUM(CASE WHEN o.tmax_c IS NOT NULL THEN sw.weight ELSE 0 END), 0)
        AS tmax_c,
    SUM(o.precip_mm * sw.weight)
        / NULLIF(SUM(CASE WHEN o.precip_mm IS NOT NULL THEN sw.weight ELSE 0 END), 0)
        AS precip_mm,
    SUM(o.humidity_pct * sw.weight)
        / NULLIF(SUM(CASE WHEN o.humidity_pct IS NOT NULL THEN sw.weight ELSE 0 END), 0)
        AS humidity_pct
FROM observations o
JOIN station_weights sw
    ON o.station_id = sw.station_id
WHERE o.source = 'sir_toscana'
  AND o.granularity = 'daily'
  AND NOT EXISTS (
      SELECT 1 FROM quality_flags qf
      WHERE qf.station_id = o.station_id
        AND qf.ts = o.ts
        AND qf.granularity = o.granularity
  )
GROUP BY sw.location_id, o.ts::DATE;
