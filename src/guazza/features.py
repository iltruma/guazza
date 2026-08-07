"""Feature engineering: costruisce la tabella features_daily in DuckDB.

features_daily è una tabella materializzata (non una view) costruita da:
  - forecasts: NWP orari aggregati a daily per (source, location_id, ts_run, target_date)
  - observations: SIR daily pesati per location via station_weights
  - climatologia: media/std mensile multi-anno calcolata dagli stessi observations

Schema di una riga: vedi SELECT finale.
  Modelli NWP: prefissi in NWP_MODEL_PREFIXES × NWP_DAILY_VARS
  Ensemble stats, obs features, climatologia, calendario, ring features, targets.

lead_time_h = ore da ts_run a mezzanotte di target_date.
I forecast orari vengono aggregati: MIN(temp)→tmin, MAX(temp)→tmax, SUM(precip), AVG(humidity/wind).

Prerequisito: station_weights deve essere popolata (guazza-forecast run la popola automaticamente).
"""

from __future__ import annotations

from guazza.storage import DuckDBClient

# Mappa modello NWP → (prefisso colonna feature, suffisso source in `forecasts`).
# Fonte unica condivisa dal pivot wide (sotto) e da FEATURE_COLS (models.py):
# derivare entrambi da qui impedisce che le due liste divergano in silenzio.
NWP_MODEL_PREFIXES: list[tuple[str, str]] = [
    ("ecmwf",  "ecmwf_ifs"),
    ("icon",   "icon_eu"),
    ("arome",  "arome_france"),
    ("icon2i", "italia_meteo_arpae_icon_2i"),
]
# Variabili NWP aggregate a daily — l'ordine fissa quello di pivot e FEATURE_COLS.
NWP_DAILY_VARS: list[str] = [
    "tmin_c", "tmax_c", "precip_mm", "humidity_pct", "wind_ms",
    "pressure_hpa_avg", "pressure_hpa_min",
    "cape_max",    # MAX giornaliero CAPE (picco pomeridiano convettivo)
]

# Colonne feature per-modello, es. "ecmwf_tmin_c". Consumate da models.FEATURE_COLS.
NWP_FEATURE_COLS: list[str] = [
    f"{prefix}_{var}" for prefix, _src in NWP_MODEL_PREFIXES for var in NWP_DAILY_VARS
]

# Blocco pivot: una colonna MAX(CASE …) per (modello × variabile), iniettato in
# _BUILD_SQL al posto del segnaposto __NWP_PIVOT_COLS__. Genera SQL identico al
# pivot esplicito precedente, mantenendo la lista derivata da NWP_MODEL_PREFIXES.
_NWP_PIVOT_COLS = ",\n        ".join(
    f"MAX(CASE WHEN source = 'open_meteo_{src}' THEN {var} END) AS {prefix}_{var}"
    for prefix, src in NWP_MODEL_PREFIXES
    for var in NWP_DAILY_VARS
)

# SELECT n.<col> per tutte le colonne NWP wide, iniettato al posto di __NWP_SELECT_COLS__.
# Deriva da NWP_FEATURE_COLS: aggiungere un modello aggiorna automaticamente anche questo.
_NWP_SELECT_COLS = ",\n    ".join(f"n.{c}" for c in NWP_FEATURE_COLS)

# Ensemble stats: (prefisso output, colonna per-modello).
# L'ordine fissa quello delle colonne nwp_*_mean/nwp_*_spread lette da models.FEATURE_COLS.
_ENSEMBLE_VARS: list[tuple[str, str]] = [
    ("tmin",     "tmin_c"),
    ("tmax",     "tmax_c"),
    ("precip",   "precip_mm"),
    ("pressure", "pressure_hpa_avg"),
    ("cape",     "cape_max"),
]


def _ensemble_block(out: str, col: str) -> str:
    """Genera il blocco SQL mean+spread per una variabile ensemble.

    Media null-safe: somma COALESCE / conteggio non-NULL.
    Spread: GREATEST-LEAST (DuckDB ignora i NULL → funziona con modelli parziali).
    I termini per-modello derivano da NWP_MODEL_PREFIXES: aggiungere un modello
    estende automaticamente la statistica.
    """
    cols = [f"n.{prefix}_{col}" for prefix, _src in NWP_MODEL_PREFIXES]
    total = " + ".join(f"COALESCE({c}, 0)" for c in cols)
    count = " + ".join(f"({c} IS NOT NULL)::INT" for c in cols)
    args = ", ".join(cols)
    return (
        f"({total}) / NULLIF({count}, 0) AS nwp_{out}_mean,\n"
        f"    GREATEST({args}) - LEAST({args}) AS nwp_{out}_spread"
    )


_ENSEMBLE_COLS = ",\n    ".join(_ensemble_block(out, col) for out, col in _ENSEMBLE_VARS)

_BUILD_SQL = """\
CREATE OR REPLACE TABLE features_daily AS
WITH
-- ── 1. Osservazioni SIR pesate per location e giorno ─────────────────────────
-- Media pesata stazione→location: definizione unica nella vista obs_weighted_daily
-- (schema.sql), condivisa con i backfill *_obs in storage.py. Alias locale per
-- leggibilità delle JOIN sottostanti (prev, tgt, climatology).
obs_weighted AS (
    SELECT location_id, obs_date, tmin_c, tmax_c, precip_mm, humidity_pct
    FROM obs_weighted_daily
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

-- ── 3. NWP orario → aggregato giornaliero ────────────────────────────────────
-- lead_time_h = giorni interi da ts_run::DATE a target_date × 24.
-- Dati same-day (backfill storico): ts_run::DATE = ts_valid::DATE → lead_time_h=0.
--   Il run nominale cambia ogni 6h (ECMWF) o 3h (ICON-EU/AROME), quindi per un dato
--   target_date esistono più ts_run sullo stesso giorno. Si aggrega tutto il giorno
--   ignorando ts_run (CASE → NULL nel GROUP BY) per ottenere tmin/tmax/precip
--   sull'intera giornata, coerente con le osservazioni SIR.
-- Dati multi-day (forecast cron in produzione): ts_valid::DATE > ts_run::DATE
--   → lead_time_h = 24, 48, ... Group by ts_run reale; last_run dedup sceglie
--   il run più recente per ogni (source, location, target_date, lead_time_h).
daily_nwp AS (
    SELECT
        source, location_id,
        CASE WHEN lead_time_days > 0 THEN ts_run ELSE NULL END AS ts_run,
        target_date,
        lead_time_days * 24 AS lead_time_h,
        MIN(temp_c)        AS tmin_c,
        MAX(temp_c)        AS tmax_c,
        SUM(precip_mm)     AS precip_mm,
        AVG(humidity_pct)  AS humidity_pct,
        AVG(wind_speed_ms) AS wind_ms,
        AVG(pressure_hpa)  AS pressure_hpa_avg,
        MIN(pressure_hpa)  AS pressure_hpa_min,
        MAX(cape_jkg)      AS cape_max
    FROM (
        SELECT
            source, location_id, ts_run,
            ts_valid::DATE                                    AS target_date,
            DATEDIFF('day', ts_run::DATE, ts_valid::DATE)    AS lead_time_days,
            temp_c, precip_mm, humidity_pct, wind_speed_ms, pressure_hpa, cape_jkg
        FROM forecasts
        WHERE ts_valid >= ts_run
    )
    GROUP BY
        source, location_id, target_date, lead_time_days,
        CASE WHEN lead_time_days > 0 THEN ts_run ELSE NULL END
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

-- ── 5. Pivot 4 modelli → wide (colonne generate da NWP_MODEL_PREFIXES) ────────
nwp_wide AS (
    SELECT
        location_id,
        target_date,
        lead_time_h,
        __NWP_PIVOT_COLS__
    FROM last_run
    GROUP BY location_id, target_date, lead_time_h
),

-- ── 6. Ring features pluviometriche (upstream spatial lag) ──────────────────
-- Stazioni SIR pluvio per distanza da ogni location (da upstream_ring_station).
-- JOIN su station_id: il location_id in observations è irrilevante qui.
ring_precip_raw AS (
    SELECT
        urs.location_id,
        o.ts::DATE   AS obs_date,
        urs.ring_label,
        AVG(o.precip_mm) AS ring_precip_mean,
        MAX(o.precip_mm) AS ring_precip_max
    FROM observations o
    JOIN upstream_ring_station urs ON o.station_id = urs.station_id
    WHERE o.source    = 'sir_toscana'
      AND o.granularity = 'daily'
      AND o.precip_mm IS NOT NULL
    GROUP BY urs.location_id, o.ts::DATE, urs.ring_label
),

ring_pivot AS (
    SELECT
        location_id,
        obs_date,
        MAX(CASE WHEN ring_label = 'ring1' THEN ring_precip_mean END) AS ring1_precip_d1_mean,
        MAX(CASE WHEN ring_label = 'ring1' THEN ring_precip_max  END) AS ring1_precip_d1_max,
        MAX(CASE WHEN ring_label = 'ring2' THEN ring_precip_mean END) AS ring2_precip_d1_mean,
        MAX(CASE WHEN ring_label = 'ring2' THEN ring_precip_max  END) AS ring2_precip_d1_max,
        MAX(CASE WHEN ring_label = 'ring3' THEN ring_precip_mean END) AS ring3_precip_d1_mean,
        MAX(CASE WHEN ring_label = 'ring3' THEN ring_precip_max  END) AS ring3_precip_d1_max
    FROM ring_precip_raw
    GROUP BY location_id, obs_date
)

-- ── 7. JOIN finale ────────────────────────────────────────────────────────────
SELECT
    n.location_id,
    n.target_date,
    n.lead_time_h,

    -- NWP per modello (colonne generate da NWP_FEATURE_COLS)
    __NWP_SELECT_COLS__,

    -- Ensemble mean/spread inter-modello (generati da _ENSEMBLE_VARS in Python).
    -- Media null-safe; spread = GREATEST-LEAST (ignora NULL → funziona con modelli parziali).
    __NWP_ENSEMBLE_COLS__,

    -- Obs features (giorno precedente — lookahead-safe)
    prev.tmin_c      AS obs_tmin_c,
    prev.tmax_c      AS obs_tmax_c,
    prev.precip_mm   AS obs_precip_mm,
    prev.humidity_pct AS obs_humidity_pct,

    -- Obs lag-2 e gradient termico (lookahead-safe: entrambi <= D-1)
    prev2.tmin_c AS obs_tmin_d2,
    prev2.tmax_c AS obs_tmax_d2,
    prev.tmin_c - prev2.tmin_c AS obs_tmin_gradient,
    prev.tmax_c - prev2.tmax_c AS obs_tmax_gradient,

    -- Anomaly (obs D-1 − clim mensile). Non in FEATURE_COLS: esperimento parcheggiato
    -- (vedi docs/archive/known_issues_resolved.md, KI-024). Mantenute perché asserite dai test.
    prev.tmin_c - c.clim_tmin_mean AS anom_tmin_c,
    prev.tmax_c - c.clim_tmax_mean AS anom_tmax_c,

    -- Climatologia mensile
    c.clim_tmin_mean, c.clim_tmin_std,
    c.clim_tmax_mean, c.clim_tmax_std,
    c.clim_precip_mean, c.clim_precip_std,

    -- Calendario
    MONTH(n.target_date)      AS month,
    DAYOFYEAR(n.target_date)  AS day_of_year,
    SIN(2*PI()*DAYOFYEAR(n.target_date)/365.25) AS doy_sin,
    COS(2*PI()*DAYOFYEAR(n.target_date)/365.25) AS doy_cos,

    -- Ring features pluviometriche (giorno precedente — lookahead-safe)
    rp.ring1_precip_d1_mean, rp.ring1_precip_d1_max,
    rp.ring2_precip_d1_mean, rp.ring2_precip_d1_max,
    rp.ring3_precip_d1_mean, rp.ring3_precip_d1_max,

    -- Target (ground truth a target_date). Tutti in valore assoluto.
    -- Le colonne anom sono un esperimento parcheggiato (vedi known_issues.md), asserite dai test.
    tgt.tmin_c - c.clim_tmin_mean AS target_tmin_anom_c,
    tgt.tmax_c - c.clim_tmax_mean AS target_tmax_anom_c,
    tgt.tmin_c                    AS target_tmin_c,
    tgt.tmax_c                    AS target_tmax_c,
    tgt.precip_mm                 AS target_precip_mm

FROM nwp_wide n
LEFT JOIN obs_weighted prev
    ON  n.location_id = prev.location_id
    AND prev.obs_date = n.target_date - INTERVAL 1 DAY
LEFT JOIN obs_weighted prev2
    ON  n.location_id = prev2.location_id
    AND prev2.obs_date = n.target_date - INTERVAL 2 DAY
LEFT JOIN obs_weighted tgt
    ON  n.location_id = tgt.location_id
    AND tgt.obs_date  = n.target_date
LEFT JOIN climatology c
    ON  n.location_id  = c.location_id
    AND MONTH(n.target_date) = c.month
LEFT JOIN ring_pivot rp
    ON  n.location_id = rp.location_id
    AND rp.obs_date   = n.target_date - INTERVAL 1 DAY
"""

_BUILD_SQL = (
    _BUILD_SQL
    .replace("__NWP_PIVOT_COLS__", _NWP_PIVOT_COLS)
    .replace("__NWP_SELECT_COLS__", _NWP_SELECT_COLS)
    .replace("__NWP_ENSEMBLE_COLS__", _ENSEMBLE_COLS)
)


def build_features_daily(db: DuckDBClient) -> int:
    """Costruisce (o ricostruisce) la tabella features_daily.

    Prerequisito: station_weights deve essere popolata.

    Returns:
        Numero di righe scritte in features_daily.
    """
    n_weights = db.execute("SELECT COUNT(*) FROM station_weights").fetchone()
    if not n_weights or n_weights[0] == 0:
        raise ValueError(
            "station_weights è vuota. "
            "Esegui prima: guazza-forecast run"
        )

    db.execute(_BUILD_SQL)

    row = db.execute("SELECT COUNT(*) FROM features_daily").fetchone()
    return int(row[0]) if row else 0
