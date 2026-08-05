"""Monitor copertura ACI: calcola coverage_30d, rileva drift, aggiorna stato ACI.

Usato da review (giornaliero) e forecast (ogni 6h).
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from guazza.models import (
    ACI_LEARNING_RATE,
    LEAD_BUCKETS,
    AdaptiveConformalizer,
    _lead_time_bucket,
)
from guazza.storage import DuckDBClient

DRIFT_TOLERANCE_PP: float = 0.05
TARGET_COVERAGE_80: float = 0.80
TARGET_COVERAGE_90: float = 0.90

# (obs_col, p10, p90, p05, p95) per ogni target — fonte unica condivisa tra compute_coverage e update_aci_from_history
TARGET_COLS: dict[str, tuple[str, str, str, str, str]] = {
    "tmin_c":    ("tmin_obs",   "tmin_p10",   "tmin_p90",   "tmin_p05",   "tmin_p95"),
    "tmax_c":    ("tmax_obs",   "tmax_p10",   "tmax_p90",   "tmax_p05",   "tmax_p95"),
    "precip_mm": ("precip_obs", "precip_p10", "precip_p90", "precip_p05", "precip_p95"),
}


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
    for target, (obs_col, p10, p90, p05, p95) in TARGET_COLS.items():
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


def update_aci_from_history(db: DuckDBClient) -> int:
    """Ricalcola lo stato ACI da tutta la history di predictions con obs.

    Legge tutte le righe predictions con *_obs valorizzato, ricostruisce
    gli AdaptiveConformalizer per ogni (target, lead_bucket) e persiste
    lo stato aggiornato in aci_state.

    Returns:
        Numero di coppie (target, bucket) aggiornate.
    """
    rows = db.execute("""
        SELECT ts_valid, lead_time_h,
               tmin_p10, tmin_p90, tmin_p05, tmin_p95, tmin_obs,
               tmax_p10, tmax_p90, tmax_p05, tmax_p95, tmax_obs,
               precip_p10, precip_p90, precip_p05, precip_p95, precip_obs
        FROM predictions
        WHERE tmin_obs IS NOT NULL OR tmax_obs IS NOT NULL OR precip_obs IS NOT NULL
        ORDER BY ts_valid
    """).df()

    if rows.empty:
        return 0

    rows = rows.assign(_bucket=rows["lead_time_h"].apply(_lead_time_bucket))
    # groupby calcolato una volta sola; sort=False preserva l'ordine ts_valid (ORDER BY in SQL)
    # — AdaptiveConformalizer.update() è sequenziale: alpha_{t+1} dipende da alpha_t
    grouped = dict(rows.groupby("_bucket", sort=False))

    n_updated = 0
    for target, (obs_col, p10_col, p90_col, p05_col, p95_col) in TARGET_COLS.items():
        for bucket in LEAD_BUCKETS:
            aci_80 = AdaptiveConformalizer(alpha_target=0.20, learning_rate=ACI_LEARNING_RATE)
            aci_90 = AdaptiveConformalizer(alpha_target=0.10, learning_rate=ACI_LEARNING_RATE)
            bsub = grouped.get(bucket)
            if bsub is not None:
                valid = bsub.dropna(subset=[obs_col, p10_col, p90_col, p05_col, p95_col])
                for covered_80, covered_90 in zip(
                    (valid[p10_col] <= valid[obs_col]) & (valid[obs_col] <= valid[p90_col]),
                    (valid[p05_col] <= valid[obs_col]) & (valid[obs_col] <= valid[p95_col]),
                    strict=True,
                ):
                    aci_80.update(bool(covered_80))
                    aci_90.update(bool(covered_90))
            db.upsert_aci_state(
                target, bucket,
                aci_80.alpha_t, aci_90.alpha_t,
                aci_80.n_updates,
                aci_80.err_sum, aci_90.err_sum,
            )
            n_updated += 1
    return n_updated
