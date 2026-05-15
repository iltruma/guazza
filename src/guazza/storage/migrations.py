"""Sistema di migrations incrementali per schema DuckDB.

`init_schema()` gestisce i CREATE TABLE IF NOT EXISTS (install da zero).
Questo modulo gestisce gli ALTER TABLE per database già esistenti.

Ogni migration è idempotente: sicura da rieseguire.
La tabella `schema_migrations` traccia quali versioni sono già state applicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from guazza.storage.duckdb_client import DuckDBClient


_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER   PRIMARY KEY,
    description VARCHAR   NOT NULL,
    applied_at  TIMESTAMP DEFAULT now()
)
"""


@dataclass
class Migration:
    version: int
    description: str
    statements: list[str] = field(default_factory=list)


# ── Registro migrations ───────────────────────────────────────────────────────
#
# Regola: aggiungi SEMPRE in coda. Non modificare migrations già rilasciate.
# Ogni statement deve essere idempotente (usa IF NOT EXISTS o ADD COLUMN IF NOT EXISTS).

MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        description=(
            "station_weights table; "
            "weight + qc_pass in observations; "
            "alpha + cost_fn + cost_fp in indicator_log"
        ),
        statements=[
            # Nuova tabella pesi stazioni (idempotente: IF NOT EXISTS)
            """
            CREATE TABLE IF NOT EXISTS station_weights (
                station_id      VARCHAR   NOT NULL,
                source          VARCHAR   NOT NULL,
                location_id     VARCHAR   NOT NULL,
                weight          DOUBLE    NOT NULL,
                distance_km     DOUBLE,
                delta_elev_m    DOUBLE,
                computed_at     TIMESTAMP DEFAULT now(),
                PRIMARY KEY (station_id, location_id)
            )
            """,
            # observations: peso stazione e flag QC Netatmo
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS weight   DOUBLE",
            "ALTER TABLE observations ADD COLUMN IF NOT EXISTS qc_pass  BOOLEAN",
            # indicator_log: parametri decision theory
            "ALTER TABLE indicator_log ADD COLUMN IF NOT EXISTS alpha    DOUBLE",
            "ALTER TABLE indicator_log ADD COLUMN IF NOT EXISTS cost_fn  DOUBLE",
            "ALTER TABLE indicator_log ADD COLUMN IF NOT EXISTS cost_fp  DOUBLE",
        ],
    ),
    Migration(
        version=2,
        description="DROP COLUMN cfr_station_id da locations (CFR rimosso — stessa rete fisica SIR)",
        statements=[
            "ALTER TABLE locations DROP COLUMN IF EXISTS cfr_station_id",
        ],
    ),
    Migration(
        version=3,
        description="netatmo_fetch_log table (Netatmo ora dinamico: no lista MAC fissa)",
        statements=[
            """
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
            )
            """,
        ],
    ),
    Migration(
        version=4,
        description=(
            "observations: PRIMARY KEY estesa a (source, station_id, location_id, ts, variable) "
            "— stessa stazione Netatmo può servire più location con pesi distinti"
        ),
        statements=[
            # Droppare l'indice PRIMA del rename (DuckDB non accetta RENAME con dipendenze)
            "DROP INDEX IF EXISTS idx_observations_location_ts",
            # Rinomina la tabella esistente
            "ALTER TABLE observations RENAME TO observations_old",
            # Ricrea con la nuova PK
            """
            CREATE TABLE observations (
                source          VARCHAR   NOT NULL,
                station_id      VARCHAR   NOT NULL,
                location_id     VARCHAR   NOT NULL,
                ts              TIMESTAMP NOT NULL,
                variable        VARCHAR   NOT NULL,
                value           DOUBLE,
                flag            VARCHAR,
                weight          DOUBLE,
                qc_pass         BOOLEAN,
                PRIMARY KEY (source, station_id, location_id, ts, variable)
            )
            """,
            # Migra i dati esistenti
            """
            INSERT INTO observations
            SELECT source, station_id, location_id, ts, variable,
                   value, flag, weight, qc_pass
            FROM observations_old
            """,
            # Ricrea indice sulla nuova tabella
            "CREATE INDEX idx_observations_location_ts ON observations (location_id, ts)",
            # Rimuovi vecchia tabella
            "DROP TABLE observations_old",
        ],
    ),
]


# ── Runner ────────────────────────────────────────────────────────────────────


def run_migrations(db: DuckDBClient) -> int:
    """Esegui le migrations pendenti sul database aperto.

    Restituisce il numero di migrations applicate in questa chiamata.
    Sicuro da chiamare ad ogni avvio: salta le migrations già applicate.
    """
    # Crea tabella di tracking se non esiste ancora
    for stmt in _BOOTSTRAP_SQL.strip().split(";"):
        if stmt.strip():
            db.execute(stmt.strip())

    applied: set[int] = {
        row[0]
        for row in db.execute("SELECT version FROM schema_migrations").fetchall()
    }

    n_applied = 0
    for migration in MIGRATIONS:
        if migration.version in applied:
            logger.debug(f"Migration v{migration.version} già applicata — skip")
            continue

        logger.info(f"Applico migration v{migration.version}: {migration.description}")
        for stmt in migration.statements:
            stmt = stmt.strip()
            if stmt:
                db.execute(stmt)

        db.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
            [migration.version, migration.description],
        )
        n_applied += 1
        logger.info(f"Migration v{migration.version} completata")

    if n_applied == 0:
        logger.info("Schema aggiornato — nessuna migration pendente")

    return n_applied
