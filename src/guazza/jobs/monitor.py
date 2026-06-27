"""Job CLI: monitoring copertura ACI e alert drift (Sprint 9).

Calcola coverage_30d per (target, lead_bucket) su predictions passate con
actual valorizzato, confronta con i target (0.80 per CI 80%, 0.90 per CI 90%),
logga warning se drift significativo.

Uso:
    uv run python -m guazza.jobs.monitor run
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer
from loguru import logger

from guazza._logging import setup_logging
from guazza.jobs._common import DB_OPTION, job_run, ping_healthchecks
from guazza.storage import DuckDBClient

app = typer.Typer(help="Monitor copertura ACI + alert drift.")


# Soglie di drift (in punti percentuali). Allert se la copertura empirica
# è FUORI da [target − DRIFT_TOLERANCE, target + DRIFT_TOLERANCE].
# Default conservativo: 5pp. Allert più aggressivo possibile riducendo.
DRIFT_TOLERANCE_PP: float = 0.05

# Target copertura per livello CI. Coerente con apply_aci_correction.
TARGET_COVERAGE_80: float = 0.80
TARGET_COVERAGE_90: float = 0.90


@dataclass
class CoverageResult:
    """Risultato coverage per (target, lead_bucket)."""
    target: str
    lead_bucket: str
    n_obs: int
    cov_80: float
    cov_90: float
    drift_80: float
    drift_90: float


@app.callback()
def _callback() -> None:
    setup_logging()


def _compute_coverage(db: DuckDBClient) -> list[CoverageResult]:
    """Calcola coverage_30d su predictions con actual.

    Returns:
        Lista di CoverageResult (uno per (target, lead_bucket)).
        `drift_80` = cov_80 − TARGET_COVERAGE_80 (positivo = over-coverage).
    """
    rows = db.execute("""
        SELECT
            tmin_obs, tmax_obs, precip_obs, lead_time_h,
            tmin_p10, tmin_p90, tmin_p05, tmin_p95,
            tmax_p10, tmax_p90, tmax_p05, tmax_p95,
            precip_p10, precip_p90, precip_p05, precip_p95
        FROM predictions
        WHERE ts_valid >= CURRENT_DATE - INTERVAL 30 DAY
          AND (tmin_obs IS NOT NULL OR tmax_obs IS NOT NULL OR precip_obs IS NOT NULL)
    """).df()

    if rows.empty:
        return []

    from guazza.models import _lead_time_bucket

    rows = rows.assign(
        bucket=rows["lead_time_h"].apply(_lead_time_bucket),
    )

    out: list[CoverageResult] = []
    for target, obs_col, p10, p90, p05, p95 in [
        ("tmin_c",    "tmin_obs",   "tmin_p10",   "tmin_p90",   "tmin_p05",   "tmin_p95"),
        ("tmax_c",    "tmax_obs",   "tmax_p10",   "tmax_p90",   "tmax_p05",   "tmax_p95"),
        ("precip_mm", "precip_obs", "precip_p10", "precip_p90", "precip_p05", "precip_p95"),
    ]:
        sub = rows.dropna(subset=[obs_col])
        if sub.empty:
            continue
        for bucket, bsub in sub.groupby("bucket"):
            cov_80 = float(((bsub[p10] <= bsub[obs_col]) & (bsub[obs_col] <= bsub[p90])).mean())
            cov_90 = float(((bsub[p05] <= bsub[obs_col]) & (bsub[obs_col] <= bsub[p95])).mean())
            out.append(CoverageResult(
                target=target,
                lead_bucket=str(bucket),
                n_obs=int(len(bsub)),
                cov_80=cov_80,
                cov_90=cov_90,
                drift_80=cov_80 - TARGET_COVERAGE_80,
                drift_90=cov_90 - TARGET_COVERAGE_90,
            ))
    return out


@app.command("run")
def cmd_run(
    db_path: Path = DB_OPTION,
    dry_run: bool = typer.Option(False, "--dry-run", help="Calcola ma non pinga Healthchecks"),
) -> None:
    """Calcola coverage_30d per (target, bucket), logga alert se drift."""
    with job_run("job_monitor") as stats:
        with DuckDBClient(db_path=db_path, read_only=True) as db:
            db.init_schema()
            results = _compute_coverage(db)

        if not results:
            logger.warning("Nessuna prediction con actual negli ultimi 30gg — skip")
            stats.summary = "no data"
            return

        n_alerts = 0
        for r in results:
            alert_80 = abs(r.drift_80) > DRIFT_TOLERANCE_PP
            alert_90 = abs(r.drift_90) > DRIFT_TOLERANCE_PP

            level = "WARN" if (alert_80 or alert_90) else "INFO"
            msg = (
                f"[{level}] {r.target} {r.lead_bucket}: n={r.n_obs} "
                f"cov80={r.cov_80:.3f} (drift {r.drift_80:+.3f}) "
                f"cov90={r.cov_90:.3f} (drift {r.drift_90:+.3f})"
            )
            if alert_80 or alert_90:
                logger.warning(msg)
                n_alerts += 1
            else:
                logger.info(msg)

        # Ping healthchecks fail se drift critico (oltre soglia +0.10pp o sotto)
        if n_alerts > 0 and not dry_run:
            ping_healthchecks("/fail")
            logger.warning(f"Drift rilevato su {n_alerts} combinazioni — healthchecks /fail")
        else:
            logger.info(f"Coverage entro tolleranza ({DRIFT_TOLERANCE_PP:.0%}) per tutti i bucket")

        stats.rows = len(results)
        stats.summary = f"{len(results)} bucket, {n_alerts} alert"


if __name__ == "__main__":
    app()
