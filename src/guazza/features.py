"""Feature engineering: costruisce la tabella features_daily in DuckDB.

features_daily è una tabella materializzata (non una view) costruita da:
  - forecasts: NWP orari aggregati a daily per (source, location_id, ts_run, target_date)
  - observations: SIR daily pesati per location via station_weights
  - climatologia: media/std mensile multi-anno calcolata dagli stessi observations

Schema di una riga:
  (location_id, target_date, lead_time_h)
  NWP per modello (ecmwf/aifs/icon/gfs/arome) × (tmin, tmax, precip, humidity, wind)
  Ensemble stats: media e spread inter-modello su tmin, tmax, precip
  Obs features: osservazione pesata del giorno prima (lookahead-safe)
  Climatologia: media/std mensile per tmin, tmax, precip
  Calendario: month, day_of_year
  Target: osservazione pesata a target_date (ground truth per il training)

lead_time_h = ore da ts_run a mezzanotte di target_date.
I forecast orari vengono aggregati: MIN(temp)→tmin, MAX(temp)→tmax, SUM(precip), AVG(humidity/wind).

Prerequisito: station_weights deve essere popolata (uv run python -m guazza.weights refresh).
"""

from __future__ import annotations

from guazza.storage import DuckDBClient

_BUILD_SQL = """\
CREATE OR REPLACE TABLE features_daily AS
WITH
-- ── 1. Osservazioni SIR pesate per location e giorno ─────────────────────────
obs_weighted AS (
    SELECT
        o.location_id,
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
       AND o.location_id = sw.location_id
    WHERE o.source = 'sir_toscana'
      AND o.granularity = 'daily'
    GROUP BY o.location_id, o.ts::DATE
),

-- ── 2. Climatologia mensile (media/std multi-anno da obs_weighted) ────────────
climatology AS (
    SELECT
        location_id,
        MONTH(obs_date)        AS month,
        AVG(tmin_c)            AS clim_tmin_mean,
        STDDEV_SAMP(tmin_c)    AS clim_tmin_std,
        AVG(tmax_c)            AS clim_tmax_mean,
        STDDEV_SAMP(tmax_c)    AS clim_tmax_std,
        AVG(precip_mm)         AS clim_precip_mean,
        STDDEV_SAMP(precip_mm) AS clim_precip_std
    FROM obs_weighted
    GROUP BY location_id, MONTH(obs_date)
),

-- ── 3. NWP orario → aggregato giornaliero per run ────────────────────────────
-- lead_time_h = ore da ts_run a mezzanotte di target_date (sempre > 0).
-- Esclusi i run dello stesso giorno di target_date (ts_valid::DATE = ts_run::DATE
-- produce lead_time_h = 0 o negativo, non utili per forecast giornaliero).
daily_nwp AS (
    SELECT
        source,
        location_id,
        ts_run,
        ts_valid::DATE                                          AS target_date,
        DATEDIFF('hour', ts_run, ts_valid::DATE::TIMESTAMP)     AS lead_time_h,
        MIN(temp_c)        AS tmin_c,
        MAX(temp_c)        AS tmax_c,
        SUM(precip_mm)     AS precip_mm,
        AVG(humidity_pct)  AS humidity_pct,
        AVG(wind_speed_ms) AS wind_ms
    FROM forecasts
    WHERE ts_valid::DATE > ts_run::DATE
    GROUP BY source, location_id, ts_run, ts_valid::DATE
),

-- ── 4. Ultimo run per (source, location_id, target_date, lead_time_h) ─────────
-- Dedup di sicurezza: due run alla stessa ora danno la stessa lead_time_h.
last_run AS (
    SELECT * EXCLUDE rn
    FROM (
        SELECT *,
            ROW_NUMBER() OVER (
                PARTITION BY source, location_id, target_date, lead_time_h
                ORDER BY ts_run DESC
            ) AS rn
        FROM daily_nwp
    )
    WHERE rn = 1
),

-- ── 5. Pivot 6 modelli → wide ─────────────────────────────────────────────────
nwp_wide AS (
    SELECT
        location_id,
        target_date,
        lead_time_h,
        MAX(CASE WHEN source = 'open_meteo_ecmwf_ifs'     THEN tmin_c END)       AS ecmwf_tmin_c,
        MAX(CASE WHEN source = 'open_meteo_ecmwf_ifs'     THEN tmax_c END)       AS ecmwf_tmax_c,
        MAX(CASE WHEN source = 'open_meteo_ecmwf_ifs'     THEN precip_mm END)    AS ecmwf_precip_mm,
        MAX(CASE WHEN source = 'open_meteo_ecmwf_ifs'     THEN humidity_pct END) AS ecmwf_humidity_pct,
        MAX(CASE WHEN source = 'open_meteo_ecmwf_ifs'     THEN wind_ms END)      AS ecmwf_wind_ms,
        MAX(CASE WHEN source = 'open_meteo_ecmwf_aifs025' THEN tmin_c END)       AS aifs_tmin_c,
        MAX(CASE WHEN source = 'open_meteo_ecmwf_aifs025' THEN tmax_c END)       AS aifs_tmax_c,
        MAX(CASE WHEN source = 'open_meteo_ecmwf_aifs025' THEN precip_mm END)    AS aifs_precip_mm,
        MAX(CASE WHEN source = 'open_meteo_ecmwf_aifs025' THEN humidity_pct END) AS aifs_humidity_pct,
        MAX(CASE WHEN source = 'open_meteo_ecmwf_aifs025' THEN wind_ms END)      AS aifs_wind_ms,
        MAX(CASE WHEN source = 'open_meteo_icon_eu'      THEN tmin_c END)       AS icon_tmin_c,
        MAX(CASE WHEN source = 'open_meteo_icon_eu'      THEN tmax_c END)       AS icon_tmax_c,
        MAX(CASE WHEN source = 'open_meteo_icon_eu'      THEN precip_mm END)    AS icon_precip_mm,
        MAX(CASE WHEN source = 'open_meteo_icon_eu'      THEN humidity_pct END) AS icon_humidity_pct,
        MAX(CASE WHEN source = 'open_meteo_icon_eu'      THEN wind_ms END)      AS icon_wind_ms,
        MAX(CASE WHEN source = 'open_meteo_icon_d2'      THEN tmin_c END)       AS icond2_tmin_c,
        MAX(CASE WHEN source = 'open_meteo_icon_d2'      THEN tmax_c END)       AS icond2_tmax_c,
        MAX(CASE WHEN source = 'open_meteo_icon_d2'      THEN precip_mm END)    AS icond2_precip_mm,
        MAX(CASE WHEN source = 'open_meteo_icon_d2'      THEN humidity_pct END) AS icond2_humidity_pct,
        MAX(CASE WHEN source = 'open_meteo_icon_d2'      THEN wind_ms END)      AS icond2_wind_ms,
        MAX(CASE WHEN source = 'open_meteo_gfs025'       THEN tmin_c END)       AS gfs_tmin_c,
        MAX(CASE WHEN source = 'open_meteo_gfs025'       THEN tmax_c END)       AS gfs_tmax_c,
        MAX(CASE WHEN source = 'open_meteo_gfs025'       THEN precip_mm END)    AS gfs_precip_mm,
        MAX(CASE WHEN source = 'open_meteo_gfs025'       THEN humidity_pct END) AS gfs_humidity_pct,
        MAX(CASE WHEN source = 'open_meteo_gfs025'       THEN wind_ms END)      AS gfs_wind_ms,
        MAX(CASE WHEN source = 'open_meteo_arome_france' THEN tmin_c END)       AS arome_tmin_c,
        MAX(CASE WHEN source = 'open_meteo_arome_france' THEN tmax_c END)       AS arome_tmax_c,
        MAX(CASE WHEN source = 'open_meteo_arome_france' THEN precip_mm END)    AS arome_precip_mm,
        MAX(CASE WHEN source = 'open_meteo_arome_france' THEN humidity_pct END) AS arome_humidity_pct,
        MAX(CASE WHEN source = 'open_meteo_arome_france' THEN wind_ms END)      AS arome_wind_ms
    FROM last_run
    GROUP BY location_id, target_date, lead_time_h
)

-- ── 6. JOIN finale ────────────────────────────────────────────────────────────
SELECT
    n.location_id,
    n.target_date,
    n.lead_time_h,

    -- NWP per modello
    n.ecmwf_tmin_c, n.ecmwf_tmax_c, n.ecmwf_precip_mm, n.ecmwf_humidity_pct, n.ecmwf_wind_ms,
    n.aifs_tmin_c,  n.aifs_tmax_c,  n.aifs_precip_mm,  n.aifs_humidity_pct,  n.aifs_wind_ms,
    n.icon_tmin_c,  n.icon_tmax_c,  n.icon_precip_mm,  n.icon_humidity_pct,  n.icon_wind_ms,
    n.icond2_tmin_c, n.icond2_tmax_c, n.icond2_precip_mm, n.icond2_humidity_pct, n.icond2_wind_ms,
    n.gfs_tmin_c,   n.gfs_tmax_c,   n.gfs_precip_mm,   n.gfs_humidity_pct,   n.gfs_wind_ms,
    n.arome_tmin_c, n.arome_tmax_c, n.arome_precip_mm, n.arome_humidity_pct, n.arome_wind_ms,

    -- Ensemble mean (media sui modelli non-null, 6 modelli)
    (COALESCE(n.ecmwf_tmin_c, 0) + COALESCE(n.aifs_tmin_c, 0) + COALESCE(n.icon_tmin_c, 0)
        + COALESCE(n.icond2_tmin_c, 0) + COALESCE(n.gfs_tmin_c, 0) + COALESCE(n.arome_tmin_c, 0))
    / NULLIF(
        (n.ecmwf_tmin_c IS NOT NULL)::INT + (n.aifs_tmin_c IS NOT NULL)::INT
        + (n.icon_tmin_c IS NOT NULL)::INT + (n.icond2_tmin_c IS NOT NULL)::INT
        + (n.gfs_tmin_c IS NOT NULL)::INT + (n.arome_tmin_c IS NOT NULL)::INT, 0
    ) AS nwp_tmin_mean,

    -- Ensemble spread (max - min tra modelli disponibili).
    -- DuckDB GREATEST/LEAST ignora i NULL: spread calcolato anche con modelli parziali.
    GREATEST(n.ecmwf_tmin_c, n.aifs_tmin_c, n.icon_tmin_c, n.icond2_tmin_c, n.gfs_tmin_c, n.arome_tmin_c)
        - LEAST(n.ecmwf_tmin_c, n.aifs_tmin_c, n.icon_tmin_c, n.icond2_tmin_c, n.gfs_tmin_c, n.arome_tmin_c)
        AS nwp_tmin_spread,

    (COALESCE(n.ecmwf_tmax_c, 0) + COALESCE(n.aifs_tmax_c, 0) + COALESCE(n.icon_tmax_c, 0)
        + COALESCE(n.icond2_tmax_c, 0) + COALESCE(n.gfs_tmax_c, 0) + COALESCE(n.arome_tmax_c, 0))
    / NULLIF(
        (n.ecmwf_tmax_c IS NOT NULL)::INT + (n.aifs_tmax_c IS NOT NULL)::INT
        + (n.icon_tmax_c IS NOT NULL)::INT + (n.icond2_tmax_c IS NOT NULL)::INT
        + (n.gfs_tmax_c IS NOT NULL)::INT + (n.arome_tmax_c IS NOT NULL)::INT, 0
    ) AS nwp_tmax_mean,

    GREATEST(n.ecmwf_tmax_c, n.aifs_tmax_c, n.icon_tmax_c, n.icond2_tmax_c, n.gfs_tmax_c, n.arome_tmax_c)
        - LEAST(n.ecmwf_tmax_c, n.aifs_tmax_c, n.icon_tmax_c, n.icond2_tmax_c, n.gfs_tmax_c, n.arome_tmax_c)
        AS nwp_tmax_spread,

    (COALESCE(n.ecmwf_precip_mm, 0) + COALESCE(n.aifs_precip_mm, 0) + COALESCE(n.icon_precip_mm, 0)
        + COALESCE(n.icond2_precip_mm, 0) + COALESCE(n.gfs_precip_mm, 0) + COALESCE(n.arome_precip_mm, 0))
    / NULLIF(
        (n.ecmwf_precip_mm IS NOT NULL)::INT + (n.aifs_precip_mm IS NOT NULL)::INT
        + (n.icon_precip_mm IS NOT NULL)::INT + (n.icond2_precip_mm IS NOT NULL)::INT
        + (n.gfs_precip_mm IS NOT NULL)::INT + (n.arome_precip_mm IS NOT NULL)::INT, 0
    ) AS nwp_precip_mean,

    GREATEST(n.ecmwf_precip_mm, n.aifs_precip_mm, n.icon_precip_mm, n.icond2_precip_mm, n.gfs_precip_mm, n.arome_precip_mm)
        - LEAST(n.ecmwf_precip_mm, n.aifs_precip_mm, n.icon_precip_mm, n.icond2_precip_mm, n.gfs_precip_mm, n.arome_precip_mm)
        AS nwp_precip_spread,

    -- Obs features (giorno precedente — lookahead-safe)
    prev.tmin_c      AS obs_tmin_c,
    prev.tmax_c      AS obs_tmax_c,
    prev.precip_mm   AS obs_precip_mm,
    prev.humidity_pct AS obs_humidity_pct,

    -- Climatologia mensile
    c.clim_tmin_mean, c.clim_tmin_std,
    c.clim_tmax_mean, c.clim_tmax_std,
    c.clim_precip_mean, c.clim_precip_std,

    -- Calendario
    MONTH(n.target_date)      AS month,
    DAYOFYEAR(n.target_date)  AS day_of_year,

    -- Target (ground truth a target_date)
    tgt.tmin_c    AS target_tmin_c,
    tgt.tmax_c    AS target_tmax_c,
    tgt.precip_mm AS target_precip_mm

FROM nwp_wide n
LEFT JOIN obs_weighted prev
    ON  n.location_id = prev.location_id
    AND prev.obs_date = n.target_date - INTERVAL 1 DAY
LEFT JOIN obs_weighted tgt
    ON  n.location_id = tgt.location_id
    AND tgt.obs_date  = n.target_date
LEFT JOIN climatology c
    ON  n.location_id  = c.location_id
    AND MONTH(n.target_date) = c.month
"""


def build_features_daily(db: DuckDBClient) -> int:
    """Costruisce (o ricostruisce) la tabella features_daily.

    Prerequisito: station_weights deve essere popolata.

    Returns:
        Numero di righe scritte in features_daily.
    """
    if db._conn is None:
        raise RuntimeError("DuckDBClient non è nel context manager.")

    n_weights = db._conn.execute(
        "SELECT COUNT(*) FROM station_weights"
    ).fetchone()
    if not n_weights or n_weights[0] == 0:
        raise ValueError(
            "station_weights è vuota. "
            "Esegui prima: uv run python -m guazza.weights refresh"
        )

    db._conn.execute(_BUILD_SQL)

    row = db._conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()
    return int(row[0]) if row else 0
