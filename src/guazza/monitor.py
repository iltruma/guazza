"""Monitor copertura ACI: calcola coverage_30d e rileva drift.

Usato dalla pipeline 6h come ultimo passo dopo predict.
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from guazza.models import _lead_time_bucket
from guazza.storage import DuckDBClient

DRIFT_TOLERANCE_PP: float = 0.05
TARGET_COVERAGE_80: float = 0.80
TARGET_COVERAGE_90: float = 0.90


@dataclass
class CoverageResult:
    target: str
    lead_bucket: str
    n_obs: int
    cov_80: float
    cov_90: float
    drift_80: float
    drift_90: float


def compute_coverage(db: DuckDBClient) -> list[CoverageResult]:
    """Calcola coverage_30d su predictions con actual valorizzato."""
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

    rows = rows.assign(bucket=rows["lead_time_h"].apply(_lead_time_bucket))

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


def check_and_log(results: list[CoverageResult]) -> int:
    """Logga i risultati, ritorna il numero di alert (drift > soglia)."""
    n_alerts = 0
    for r in results:
        alert_80 = abs(r.drift_80) > DRIFT_TOLERANCE_PP
        alert_90 = abs(r.drift_90) > DRIFT_TOLERANCE_PP
        msg = (
            f"monitor {r.target} {r.lead_bucket}: n={r.n_obs} "
            f"cov80={r.cov_80:.3f} (drift {r.drift_80:+.3f}) "
            f"cov90={r.cov_90:.3f} (drift {r.drift_90:+.3f})"
        )
        if alert_80 or alert_90:
            logger.warning(msg)
            n_alerts += 1
        else:
            logger.info(msg)
    return n_alerts
