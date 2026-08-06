"""Test della logica di curva skill (jobs.review._curve_for) — no DB, no training."""

from __future__ import annotations

import pandas as pd

from guazza.jobs.review import LEADS, MIN_SAMPLES_PER_LEAD, _curve_for


def _row(lead: int, pred: float, nwp: float, obs: float) -> dict[str, object]:
    return {"lead_time_h": lead, "tmax_p50": pred, "nwp_tmax_mean": nwp, "tmax_obs": obs}


def test_curve_computes_mae_and_skill_for_populated_lead() -> None:
    # Lead 24: Guazza MAE 0.5, NWP MAE 1.0 → skill +50%; campioni sufficienti.
    rows = [_row(24, pred=10.5, nwp=11.0, obs=10.0) for _ in range(MIN_SAMPLES_PER_LEAD)]
    curve = _curve_for(pd.DataFrame(rows), "tmax_c")

    p24 = next(p for p in curve if p["lead_h"] == 24)
    assert p24["n"] == MIN_SAMPLES_PER_LEAD
    assert p24["mae_ml"] == 0.5
    assert p24["mae_nwp"] == 1.0
    assert p24["skill_pct"] == 50.0


def test_curve_returns_all_leads() -> None:
    curve = _curve_for(pd.DataFrame([_row(24, 10.0, 11.0, 10.0)]), "tmax_c")
    assert [p["lead_h"] for p in curve] == LEADS


def test_curve_nulls_below_min_samples() -> None:
    # Un solo campione sul lead 0 → sotto soglia → metriche null ma n riportato.
    rows = [_row(0, pred=10.0, nwp=11.0, obs=10.0)]
    curve = _curve_for(pd.DataFrame(rows), "tmax_c")

    p0 = next(p for p in curve if p["lead_h"] == 0)
    assert p0["n"] == 1
    assert p0["mae_ml"] is None
    assert p0["skill_pct"] is None


def test_curve_ignores_rows_with_null_ground_truth() -> None:
    # Righe senza obs non contano nel campione.
    rows: list[dict[str, object]] = [_row(24, 10.5, 11.0, 10.0) for _ in range(MIN_SAMPLES_PER_LEAD)]
    rows.append({"lead_time_h": 24, "tmax_p50": 9.0, "nwp_tmax_mean": 11.0, "tmax_obs": None})
    curve = _curve_for(pd.DataFrame(rows), "tmax_c")

    p24 = next(p for p in curve if p["lead_h"] == 24)
    assert p24["n"] == MIN_SAMPLES_PER_LEAD
