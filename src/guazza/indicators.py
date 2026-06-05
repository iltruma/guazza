"""Decision Logic Engine — valutazione indicatori operativi semaforo.

Input:  SignalBag (dict str→float) pre-computato dal pipeline di ingestion/forecast.
Output: lista IndicatorResult (verde/giallo/rosso) + log in DuckDB indicator_log.

CLI:
    uv run python -m guazza.indicators evaluate --location casa_campi --signals '{...}'
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import yaml
from loguru import logger

if TYPE_CHECKING:
    from guazza.storage import DuckDBClient

SignalBag = dict[str, float | None]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_INDICATORS_YAML = Path(
    os.environ.get("CONFIG_DIR", str(_REPO_ROOT / "config"))
) / "indicators.yaml"


@dataclass
class IndicatorResult:
    indicator_id: str
    location_id: str
    verdict: str              # 'verde' | 'giallo' | 'rosso'
    rule_matched: str         # 'green' | 'yellow' | 'red' | 'fallback'
    rule_text: str            # condizione YAML che ha matchato (human-readable nel frontend)
    alpha: float              # cost_fp / (cost_fp + cost_fn)
    cost_fn: float
    cost_fp: float
    probability: float | None = None
    ts: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


def load_indicators(path: Path | None = None) -> dict[str, Any]:
    """Carica la sezione 'indicators' dal YAML."""
    p = path or _DEFAULT_INDICATORS_YAML
    with p.open() as f:
        return yaml.safe_load(f)["indicators"]  # type: ignore[no-any-return]


def _compute_alpha(costs: dict[str, Any]) -> float:
    """alpha = cost_fp / (cost_fp + cost_fn)."""
    fn = float(costs["fn"])
    fp = float(costs["fp"])
    return fp / (fp + fn)


def _eval_condition(logic_str: str, signals: SignalBag) -> bool:
    """Valuta una condizione logica YAML contro un SignalBag."""
    if not logic_str:
        return False
    expr = logic_str.replace(" AND ", " and ").replace(" OR ", " or ")
    expr = re.sub(
        r"P\([^)]+\)",
        lambda m: f"_sig.get({m.group()!r}, 0.0)",
        expr,
    )
    try:
        clean = {k: (v if v is not None else 0.0) for k, v in signals.items()}
        return bool(eval(expr, {"__builtins__": {}}, {**clean, "_sig": clean}))  # noqa: S307
    except Exception as exc:
        logger.warning(f"Errore eval condizione '{logic_str}': {exc}")
        return False


def evaluate_indicator(
    indicator_id: str,
    cfg: dict[str, Any],
    signals: SignalBag,
    location_id: str,
) -> IndicatorResult | None:
    """Valuta un singolo indicatore. Restituisce None se non si applica alla location."""
    location_specific: list[str] | None = cfg.get("location_specific")
    if location_specific and location_id not in location_specific:
        return None

    costs = cfg.get("costs", {"fn": 1, "fp": 1})
    alpha = _compute_alpha(costs)
    logic: dict[str, str] = cfg.get("logic", {})

    static_thresholds = {k: float(v) for k, v in cfg.get("thresholds", {}).items()}
    effective_signals: SignalBag = {**signals, **static_thresholds}

    # Segnali indispensabili: se mancano (assenti o None) il verdetto è indecidibile.
    # Senza questo, una regola come "level_sir < soglia" valuterebbe il None come 0.0
    # → falso "verde nella norma", oppure (segnale assente) cadrebbe nel fallback giallo.
    required: list[str] = cfg.get("requires", [])
    if any(effective_signals.get(s) is None for s in required):
        return IndicatorResult(
            indicator_id=indicator_id,
            location_id=location_id,
            verdict="grigio",
            rule_matched="unknown",
            rule_text="Dato non disponibile",
            alpha=alpha,
            cost_fn=float(costs["fn"]),
            cost_fp=float(costs["fp"]),
        )

    for rule in ("red", "yellow", "green"):
        condition = logic.get(rule, "")
        if condition and _eval_condition(condition, effective_signals):
            verdict_it = {"green": "verde", "yellow": "giallo", "red": "rosso"}[rule]
            return IndicatorResult(
                indicator_id=indicator_id,
                location_id=location_id,
                verdict=verdict_it,
                rule_matched=rule,
                rule_text=logic.get(f"{rule}_desc", condition),
                alpha=alpha,
                cost_fn=float(costs["fn"]),
                cost_fp=float(costs["fp"]),
            )

    logger.warning(
        f"[{location_id}] {indicator_id}: nessuna condizione soddisfatta → fallback giallo"
    )
    return IndicatorResult(
        indicator_id=indicator_id,
        location_id=location_id,
        verdict="giallo",
        rule_matched="fallback",
        rule_text="Nessuna condizione soddisfatta",
        alpha=alpha,
        cost_fn=float(costs["fn"]),
        cost_fp=float(costs["fp"]),
    )


def evaluate_all(
    indicators: dict[str, Any],
    signals: SignalBag,
    location_id: str,
) -> list[IndicatorResult]:
    """Valuta tutti gli indicatori applicabili per una location."""
    results: list[IndicatorResult] = []
    for ind_id, cfg in indicators.items():
        result = evaluate_indicator(ind_id, cfg, signals, location_id)
        if result is not None:
            results.append(result)
    return results


def log_results(
    db: DuckDBClient,
    results: list[IndicatorResult],
    input_summary: dict[str, Any] | None = None,
) -> None:
    """Salva i risultati DLE in indicator_log. Idempotente per stessa (ts, location, indicator)."""
    if not results:
        return
    summary_json = json.dumps(input_summary or {})
    df = pd.DataFrame(
        [[r.ts, r.location_id, r.indicator_id, summary_json,
          r.rule_matched, r.verdict, r.probability, r.alpha, r.cost_fn, r.cost_fp]
         for r in results],
        columns=["ts", "location_id", "indicator_id", "input_summary",
                 "rule_matched", "verdict", "probability", "alpha", "cost_fn", "cost_fp"],
    )
    db.register_df("_stg_ind", df)
    db.execute("""
        INSERT INTO indicator_log
            (ts, location_id, indicator_id, input_summary, rule_matched,
             verdict, probability, alpha, cost_fn, cost_fp)
        SELECT ts, location_id, indicator_id, input_summary, rule_matched,
               verdict, probability, alpha, cost_fn, cost_fp
        FROM _stg_ind
        ON CONFLICT (ts, location_id, indicator_id) DO UPDATE SET
            rule_matched = excluded.rule_matched,
            verdict      = excluded.verdict,
            probability  = excluded.probability,
            alpha        = excluded.alpha,
            cost_fn      = excluded.cost_fn,
            cost_fp      = excluded.cost_fp
    """)
    db.unregister_df("_stg_ind")
    logger.info(
        f"indicator_log: {len(results)} record salvati "
        f"[{results[0].location_id} @ {results[0].ts:%Y-%m-%dT%H:%M}]"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

import typer  # noqa: E402

app = typer.Typer(help="Decision Logic Engine — valutazione manuale indicatori.")


@app.command("evaluate")
def cmd_evaluate(
    location: str = typer.Option(..., help="ID location (es. casa_campi)"),
    signals_json: str = typer.Option("{}", "--signals", help="JSON con SignalBag"),
    config_dir: str = typer.Option(str(_DEFAULT_INDICATORS_YAML.parent), "--config-dir"),
) -> None:
    """Valuta tutti gli indicatori per una location con i segnali forniti."""
    signals: SignalBag = json.loads(signals_json)
    indicators = load_indicators(Path(config_dir) / "indicators.yaml")
    results = evaluate_all(indicators, signals, location)

    verdict_icon = {"verde": "🟢", "giallo": "🟡", "rosso": "🔴", "grigio": "⚪"}
    typer.echo(f"\n{location} — {len(results)} indicatori:")
    for r in results:
        icon = verdict_icon.get(r.verdict, "⚪")
        typer.echo(
            f"  {icon} {r.indicator_id:<12} {r.verdict:<6}  "
            f"alpha={r.alpha:.2f}  rule={r.rule_matched}"
        )


if __name__ == "__main__":
    app()
