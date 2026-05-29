"""DuckDB client con lock file per scritture serializzate.

Workaround KI001: DuckDB ammette un solo writer alla volta.
Usiamo fcntl.flock() su un lock file per serializzare le aperture in write mode.
In read-only mode il lock non è necessario.

Uso tipico:
    with DuckDBClient() as db:
        db.execute("INSERT INTO ...")
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import typer
from loguru import logger

_DEFAULT_DB_PATH = Path(os.environ.get("DB_PATH", "/var/lib/guazza/guazza.duckdb"))
_SCHEMA_SQL = Path(__file__).parent / "schema.sql"


class DuckDBClient:
    """Wrapper attorno a duckdb.connect con lock file per write serializzate."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        read_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self.read_only = read_only
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._lock_fd: int | None = None
        self._lock_path = self.db_path.with_suffix(".lock")

    def __enter__(self) -> DuckDBClient:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.read_only:
            self._lock_fd = os.open(str(self._lock_path), os.O_CREAT | os.O_WRONLY)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            logger.debug(f"Lock acquisito: {self._lock_path}")

        self._conn = duckdb.connect(str(self.db_path), read_only=self.read_only)
        logger.debug(f"DuckDB aperto: {self.db_path} (read_only={self.read_only})")
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None
            logger.debug(f"Lock rilasciato: {self._lock_path}")

    def execute(self, query: str, params: list[Any] | None = None) -> Any:
        """Esegui una query SQL. Richiede connessione aperta."""
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")
        if params:
            return self._conn.execute(query, params)
        return self._conn.execute(query)

    def executemany(self, query: str, params: list[list[Any]]) -> None:
        """Esegui la stessa query su una lista di parametri (batch insert)."""
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")
        self._conn.executemany(query, params)

    def register_df(self, name: str, df: pd.DataFrame) -> None:
        """Espone un DataFrame come relazione virtuale (path Arrow, senza staging table)."""
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")
        self._conn.register(name, df)

    def unregister_df(self, name: str) -> None:
        """Rimuove la relazione virtuale registrata con register_df."""
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")
        self._conn.unregister(name)

    def ensure_predictions_schema(self) -> None:
        """Ricrea predictions se lo schema è obsoleto (pre-v0.5, manca tmin_p05)."""
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")
        try:
            self._conn.execute("SELECT tmin_p05 FROM predictions LIMIT 0")
        except duckdb.Error:
            self._conn.execute("DROP TABLE IF EXISTS predictions")
            sql = _SCHEMA_SQL.read_text()
            self._conn.execute(sql)
            logger.info("predictions: schema migrato alla versione corrente (v0.5)")

    def upsert_predictions(self, records: list[dict]) -> int:
        """UPSERT batch per predictions ML.

        Ogni record deve avere: model_version, location_id, ts_valid, lead_time_h,
        più i dict annidati tmin_c, tmax_c, precip_mm con chiavi p05/p10/.../ci80_lo/...

        Returns:
            Numero di record processati.
        """
        if not records:
            return 0
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")

        _PRED_COLS = [
            "model_version", "location_id", "ts_valid", "lead_time_h",
            "tmin_p05", "tmin_p10", "tmin_p50", "tmin_p90", "tmin_p95",
            "tmin_ci80_lo", "tmin_ci80_hi", "tmin_ci90_lo", "tmin_ci90_hi",
            "tmax_p05", "tmax_p10", "tmax_p50", "tmax_p90", "tmax_p95",
            "tmax_ci80_lo", "tmax_ci80_hi", "tmax_ci90_lo", "tmax_ci90_hi",
            "precip_p05", "precip_p10", "precip_p50", "precip_p90", "precip_p95",
            "precip_ci80_lo", "precip_ci80_hi", "precip_ci90_lo", "precip_ci90_hi",
        ]

        rows = []
        for rec in records:
            ts = rec["ts_valid"]
            if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            tmin = rec["tmin_c"]
            tmax = rec["tmax_c"]
            prec = rec["precip_mm"]
            rows.append([
                rec["model_version"], rec["location_id"], ts, rec["lead_time_h"],
                tmin.get("p05"), tmin.get("p10"), tmin.get("p50"), tmin.get("p90"), tmin.get("p95"),
                tmin.get("ci80_lo"), tmin.get("ci80_hi"), tmin.get("ci90_lo"), tmin.get("ci90_hi"),
                tmax.get("p05"), tmax.get("p10"), tmax.get("p50"), tmax.get("p90"), tmax.get("p95"),
                tmax.get("ci80_lo"), tmax.get("ci80_hi"), tmax.get("ci90_lo"), tmax.get("ci90_hi"),
                prec.get("p05"), prec.get("p10"), prec.get("p50"), prec.get("p90"), prec.get("p95"),
                prec.get("ci80_lo"), prec.get("ci80_hi"), prec.get("ci90_lo"), prec.get("ci90_hi"),
            ])

        df = pd.DataFrame(rows, columns=_PRED_COLS)
        self._conn.register("_staging_pred", df)
        self._conn.execute("""
            INSERT OR REPLACE INTO predictions (
                model_version, location_id, ts_valid, lead_time_h,
                tmin_p05, tmin_p10, tmin_p50, tmin_p90, tmin_p95,
                tmin_ci80_lo, tmin_ci80_hi, tmin_ci90_lo, tmin_ci90_hi,
                tmax_p05, tmax_p10, tmax_p50, tmax_p90, tmax_p95,
                tmax_ci80_lo, tmax_ci80_hi, tmax_ci90_lo, tmax_ci90_hi,
                precip_p05, precip_p10, precip_p50, precip_p90, precip_p95,
                precip_ci80_lo, precip_ci80_hi, precip_ci90_lo, precip_ci90_hi
            )
            SELECT * FROM _staging_pred
        """)
        self._conn.unregister("_staging_pred")
        logger.info(f"upsert_predictions: {len(records)} record salvati")
        return len(records)

    def backfill_prediction_obs(self) -> int:
        """Aggiorna *_obs nelle predictions passate con le osservazioni SIR pesate.

        Idempotente: aggiorna solo le righe con tmin_obs IS NULL.

        Returns:
            Numero approssimativo di righe aggiornate.
        """
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")
        result = self._conn.execute("""
            UPDATE predictions
            SET
                tmin_obs   = ow.tmin_c,
                tmax_obs   = ow.tmax_c,
                precip_obs = ow.precip_mm
            FROM (
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
                        AS precip_mm
                FROM observations o
                JOIN station_weights sw
                    ON o.station_id = sw.station_id AND o.location_id = sw.location_id
                WHERE o.source = 'sir_toscana' AND o.granularity = 'daily'
                GROUP BY o.location_id, o.ts::DATE
            ) ow
            WHERE predictions.location_id = ow.location_id
              AND predictions.ts_valid::DATE = ow.obs_date
              AND predictions.tmin_obs IS NULL
        """)
        n = result.fetchone()
        count = int(n[0]) if n else 0
        if count:
            logger.info(f"backfill_prediction_obs: {count} righe aggiornate")
        return count

    def init_schema(self) -> None:
        """Applica schema.sql al database (IF NOT EXISTS — idempotente)."""
        if not _SCHEMA_SQL.exists():
            raise FileNotFoundError(f"Schema SQL non trovato: {_SCHEMA_SQL}")
        sql = _SCHEMA_SQL.read_text()
        # Eseguiamo lo script intero: DuckDB accetta più statement separati da ";"
        # in una singola execute(). Split manuale su ";" è fragile con MACRO e commenti.
        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")
        self._conn.execute(sql)
        self._ensure_aq_columns()
        self._ensure_forecast_columns()
        logger.info("Schema applicato")

    def _ensure_aq_columns(self) -> None:
        """Aggiunge colonne AQ a observations se mancanti (migrazione idempotente)."""
        for col, dtype in [
            ("co_mgm3",      "DOUBLE"),
            ("benzene_ugm3", "DOUBLE"),
            ("so2_ugm3",     "DOUBLE"),
        ]:
            self.execute(
                f"ALTER TABLE observations ADD COLUMN IF NOT EXISTS {col} {dtype}"
            )

    def _ensure_forecast_columns(self) -> None:
        """Aggiunge colonne a forecasts se mancanti (migrazione idempotente)."""
        self.execute(
            "ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS weather_code INTEGER"
        )

    def verify_schema(self) -> bool:
        """Verifica che le tabelle attese esistano nel database."""
        expected_tables = {
            "locations",
            "observations",
            "forecasts",
            "predictions",
            "benchmark_forecasts",
            "alerts",
            "station_weights",
            "netatmo_fetch_log",
            "indicator_log",
            "quality_flags",
        }
        result = self.execute("SHOW TABLES").fetchall()
        existing = {row[0] for row in result}
        missing = expected_tables - existing
        if missing:
            logger.error(f"Tabelle mancanti: {missing}")
            return False
        logger.info(f"Schema OK: {len(existing)} tabelle presenti")
        return True

    def upsert_forecasts(self, records: list[dict]) -> int:
        """UPSERT batch wide per forecast Open-Meteo nella tabella forecasts.

        PK: (source, location_id, ts_run, ts_valid).
        DO UPDATE sovrascrive tutte le colonne meteo (l'ultimo run vince).
        Usa staging table + bulk UPDATE/INSERT per performance su backfill storico:
        - executemany sulla staging (no indici → veloce)
        - UPDATE bulk per righe esistenti
        - INSERT bulk per righe nuove
        ~10-50x più veloce di executemany+ON CONFLICT su batch grandi.

        Returns:
            Numero di record processati.
        """
        if not records:
            return 0

        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")

        seen: set[tuple] = set()
        rows = []
        for rec in records:
            # Normalize to UTC-naive: prevents TIMESTAMPTZ→TIMESTAMP implicit conversion
            # by DuckDB using session timezone (would collapse DST-ambiguous hours).
            ts_run = rec["ts_run"].replace(tzinfo=None)
            ts_valid = rec["ts_valid"].replace(tzinfo=None)
            key = (rec["source"], rec["location_id"], ts_run, ts_valid)
            if key in seen:
                continue
            seen.add(key)
            rows.append([
                rec["source"], rec["location_id"], ts_run, ts_valid,
                rec.get("lead_time_h"),
                rec.get("temp_c"), rec.get("humidity_pct"), rec.get("precip_mm"),
                rec.get("wind_speed_ms"), rec.get("wind_dir_deg"),
                rec.get("wind_gust_ms"), rec.get("pressure_hpa"),
                rec.get("weather_code"),
            ])

        _FCAST_COLS = [
            "source", "location_id", "ts_run", "ts_valid", "lead_time_h",
            "temp_c", "humidity_pct", "precip_mm",
            "wind_speed_ms", "wind_dir_deg", "wind_gust_ms", "pressure_hpa",
            "weather_code",
        ]
        df = pd.DataFrame(rows, columns=_FCAST_COLS)
        self._conn.register("_staging_forecasts", df)

        # INSERT OR REPLACE: gestisce sia insert nuovi sia update esistenti in un solo
        # statement. Evita completamente il WHERE NOT EXISTS, che confronta TIMESTAMPTZ
        # (staging) con TIMESTAMP (forecasts) e fallisce sui timestamp UTC non-zero
        # (es. DST-transition days).
        self._conn.execute("""
            INSERT OR REPLACE INTO forecasts (
                source, location_id, ts_run, ts_valid, lead_time_h,
                temp_c, humidity_pct, precip_mm,
                wind_speed_ms, wind_dir_deg, wind_gust_ms, pressure_hpa,
                weather_code
            )
            SELECT
                source, location_id, ts_run, ts_valid, lead_time_h,
                temp_c, humidity_pct, precip_mm,
                wind_speed_ms, wind_dir_deg, wind_gust_ms, pressure_hpa,
                weather_code
            FROM (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY source, location_id, ts_run, ts_valid
                       ) AS _rn
                FROM _staging_forecasts
            ) s
            WHERE s._rn = 1
        """)

        self._conn.unregister("_staging_forecasts")
        logger.info(f"upsert_forecasts: {len(records)} record processati")
        return len(records)

    def upsert_sir_observations(self, records: list[dict]) -> int:
        """UPSERT wide per osservazioni SIR/ARPAT/Netatmo storiche.

        Ogni record è parziale (solo le colonne del sensore scaricato).
        Usa staging table + UPDATE/INSERT per performance bulk:
        - Righe esistenti: aggiornate con COALESCE (preserva non-NULL)
        - Righe nuove: inserite

        ~10-50x più veloce del precedente executemany+ON CONFLICT su batch grandi.

        Returns:
            Numero di record processati.
        """
        if not records:
            return 0

        if self._conn is None:
            raise RuntimeError("DuckDBClient non è nel context manager.")

        # Solo le colonne che esistono nello schema (escluse PK)
        _obs_cols = [
            "tmax_c", "tmin_c", "temp_c",
            "humidity_pct",
            "precip_mm", "precip_interval_h",
            "wind_speed_ms", "wind_dir_deg", "wind_gust_ms",
            "pressure_hpa", "level_m",
            "pm10_ugm3", "pm25_ugm3", "no2_ugm3", "o3_ugm3",
            "co_mgm3", "benzene_ugm3", "so2_ugm3",
            "weight", "qc_pass",
        ]

        # Prepara righe
        rows: list[list[Any]] = []
        for rec in records:
            humidity = (
                rec.get("hum_med_pct")
                if rec.get("hum_med_pct") is not None
                else rec.get("humidity_pct")
            )
            ts = rec["ts"]
            if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            rows.append([
                rec.get("source", "sir_toscana"),
                rec["station_id"],
                rec.get("location_id", ""),
                ts,
                rec["granularity"],
                rec.get("tmax_c"),
                rec.get("tmin_c"),
                rec.get("temp_c"),
                humidity,
                rec.get("precip_mm"),
                rec.get("precip_interval_h"),
                rec.get("wind_speed_ms"),
                rec.get("wind_dir_deg"),
                rec.get("wind_gust_ms"),
                rec.get("pressure_hpa"),
                rec.get("level_m"),
                rec.get("pm10_ugm3"),
                rec.get("pm25_ugm3"),
                rec.get("no2_ugm3"),
                rec.get("o3_ugm3"),
                rec.get("co_mgm3"),
                rec.get("benzene_ugm3"),
                rec.get("so2_ugm3"),
                rec.get("weight"),
                rec.get("qc_pass"),
            ])

        # Dedup in-batch: PK = (source, station_id, ts, granularity) = indici 0,1,3,4
        # Ultimo record vince con merge COALESCE.
        dedup: dict[tuple[Any, ...], list[Any]] = {}
        for row in rows:
            pk = (row[0], row[1], row[3], row[4])
            if pk in dedup:
                existing = dedup[pk]
                merged = [
                    new if new is not None else old
                    for old, new in zip(existing, row, strict=True)
                ]
                dedup[pk] = merged
            else:
                dedup[pk] = row
        rows = list(dedup.values())

        # ── Staging DataFrame + bulk UPDATE/INSERT ──────────────────────────
        _OBS_STAGING_COLS = [
            "source", "station_id", "location_id", "ts", "granularity",
            "tmax_c", "tmin_c", "temp_c", "humidity_pct",
            "precip_mm", "precip_interval_h",
            "wind_speed_ms", "wind_dir_deg", "wind_gust_ms",
            "pressure_hpa", "level_m",
            "pm10_ugm3", "pm25_ugm3", "no2_ugm3", "o3_ugm3",
            "co_mgm3", "benzene_ugm3", "so2_ugm3",
            "weight", "qc_pass",
        ]
        df = pd.DataFrame(rows, columns=_OBS_STAGING_COLS)
        self._conn.register("_staging_obs", df)

        # UPDATE righe esistenti con COALESCE
        coalesce_sets = ", ".join(
            f"{col} = COALESCE(s.{col}, observations.{col})"
            for col in _obs_cols
        )
        self._conn.execute(f"""
            UPDATE observations SET
                location_id   = COALESCE(s.location_id, observations.location_id),
                {coalesce_sets},
                last_modified = current_timestamp
            FROM _staging_obs s
            WHERE observations.source = s.source
              AND observations.station_id = s.station_id
              AND observations.ts = s.ts
              AND observations.granularity = s.granularity
        """)

        # INSERT righe nuove (non presenti in observations)
        self._conn.execute("""
            INSERT INTO observations
                (source, station_id, location_id, ts, granularity,
                 tmax_c, tmin_c, temp_c,
                 humidity_pct, precip_mm, precip_interval_h,
                 wind_speed_ms, wind_dir_deg, wind_gust_ms,
                 pressure_hpa, level_m,
                 pm10_ugm3, pm25_ugm3, no2_ugm3, o3_ugm3,
                 co_mgm3, benzene_ugm3, so2_ugm3,
                 weight, qc_pass)
            SELECT s.source, s.station_id, s.location_id, s.ts, s.granularity,
                   s.tmax_c, s.tmin_c, s.temp_c,
                   s.humidity_pct, s.precip_mm, s.precip_interval_h,
                   s.wind_speed_ms, s.wind_dir_deg, s.wind_gust_ms,
                   s.pressure_hpa, s.level_m,
                   s.pm10_ugm3, s.pm25_ugm3, s.no2_ugm3, s.o3_ugm3,
                   s.co_mgm3, s.benzene_ugm3, s.so2_ugm3,
                   s.weight, s.qc_pass
            FROM _staging_obs s
            WHERE NOT EXISTS (
                SELECT 1 FROM observations o
                WHERE o.source = s.source
                  AND o.station_id = s.station_id
                  AND o.ts = s.ts
                  AND o.granularity = s.granularity
            )
        """)

        self._conn.unregister("_staging_obs")

        logger.info(f"upsert_sir_observations: {len(records)} record processati")
        return len(records)


@contextmanager
def open_db(
    db_path: Path | str | None = None,
    read_only: bool = False,
) -> Generator[DuckDBClient, None, None]:
    """Shortcut: `with open_db() as db:` invece di instanziare direttamente."""
    client = DuckDBClient(db_path=db_path, read_only=read_only)
    with client:
        yield client


# ── CLI entry point ───────────────────────────────────────────────────────────

app = typer.Typer(help="Utility DuckDB per Guazza.")

_DB_OPTION = typer.Option(_DEFAULT_DB_PATH, "--db", help="Path del file DuckDB")


@app.command("init-schema")
def cmd_init_schema(db_path: Path = _DB_OPTION) -> None:
    """Inizializza (o aggiorna) lo schema DuckDB."""
    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
    typer.echo("Schema inizializzato.")


@app.command("verify-schema")
def cmd_verify_schema(db_path: Path = _DB_OPTION) -> None:
    """Verifica che tutte le tabelle attese esistano."""
    with DuckDBClient(db_path=db_path, read_only=True) as db:
        ok = db.verify_schema()
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
