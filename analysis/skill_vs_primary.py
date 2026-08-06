"""Robustness check — skill ML vs NWP misurato contro la stazione SIR primaria.

Estende D-016. Lo skill di Sprint 4/CV (+32/+43%) è calcolato contro il **target
pesato** (il target di training del modello): internamente coerente, ma un revisore può
obiettare che il NWP è svantaggiato perché non punta al blend multi-stazione. Qui sia il
modello ML sia l'ensemble NWP-mean vengono valutati contro il **gauge fisico indipendente**
(stazione SIR primaria di ogni location), out-of-sample, con lo stesso split walk-forward +
embargo di `walk_forward_cv`. È la claim più difendibile per il case study.

Il modello resta addestrato sul target pesato (è il prodotto): cambia solo il ground truth
di **valutazione**. Si usa solo la mediana q=0.50 (la MAE non richiede i quantili/CQR).

Read-only sul DB. Nessuna scrittura DuckDB.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import typer

from guazza.models import _TARGET_NWP_MEAN, FEATURE_COLS, TARGETS, train_lgbm
from guazza.weights import primary_stations

app = typer.Typer(add_completion=False)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO / "data" / "guazza.duckdb"


def _load_features(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute("SELECT * FROM features_daily").df()
    df["location_id"] = df["location_id"].astype("category")
    df["target_date"] = pd.to_datetime(df["target_date"]).dt.date
    return df.sort_values("target_date").reset_index(drop=True)


def _load_primary_obs(con: duckdb.DuckDBPyConnection, stations: dict[str, str]) -> pd.DataFrame:
    """Osservazioni daily della stazione primaria per location (giorno di calendario)."""
    station_map = ", ".join(f"('{loc}','{st}')" for loc, st in stations.items())
    df = con.execute(f"""
        WITH st(location_id, station_id) AS (VALUES {station_map})
        SELECT st.location_id, o.ts::date AS target_date,
               o.tmin_c AS prim_tmin_c, o.tmax_c AS prim_tmax_c, o.precip_mm AS prim_precip_mm
        FROM observations o JOIN st ON o.station_id = st.station_id
        WHERE o.source = 'sir_toscana' AND o.granularity = 'daily'
    """).df()
    df["target_date"] = pd.to_datetime(df["target_date"]).dt.date
    return df


def _walk_forward_predictions(
    df: pd.DataFrame,
    n_splits: int = 4,
    min_train_days: int = 365,
    embargo_days: int = 7,
) -> pd.DataFrame:
    """Predizioni p50 out-of-sample con lo stesso split di models.walk_forward_cv.

    Restituisce, per ogni riga di test, le colonne chiave + pred_<target> (mediana ML)
    e nwp_<target> (ensemble mean). Il target di training resta quello pesato.
    """
    dates = sorted(df["target_date"].unique())
    available_start = min_train_days + embargo_days
    if len(dates) <= available_start:
        raise ValueError(f"Dati insufficienti: {len(dates)} date, richieste >{available_start}")

    fold_dates = dates[available_start:]
    fold_size = max(30, len(fold_dates) // n_splits)
    out: list[pd.DataFrame] = []

    for i in range(n_splits):
        test_start = fold_dates[i * fold_size]
        end_idx = min((i + 1) * fold_size, len(fold_dates)) - 1
        test_end = fold_dates[end_idx]
        train_end = (pd.Timestamp(test_start) - pd.Timedelta(days=embargo_days)).date()

        df_train = df[df["target_date"] <= train_end]
        df_test = df[(df["target_date"] >= test_start) & (df["target_date"] <= test_end)]
        if df_train.empty or df_test.empty:
            continue

        keep = ["location_id", "target_date", "lead_time_h"]
        block = df_test[keep].copy()
        for target in TARGETS:
            col = f"target_{target}"
            mask = df_train[col].notna()
            if mask.sum() < 50:
                continue
            model = train_lgbm(df_train.loc[mask, FEATURE_COLS], df_train.loc[mask, col], 0.50)
            block[f"pred_{target}"] = model.predict(df_test[FEATURE_COLS])
            block[f"nwp_{target}"] = df_test[_TARGET_NWP_MEAN[target]].values
            block[f"wgt_{target}"] = df_test[col].values
        out.append(block)
        typer.echo(f"  fold {i+1}/{n_splits}: train≤{train_end} test {test_start}→{test_end} "
                   f"({len(df_test)} righe)")

    return pd.concat(out, ignore_index=True)


def _skill_table(preds: pd.DataFrame, var: str) -> pd.DataFrame:
    """Per (location): MAE NWP vs ML contro stazione primaria + skill. Vs pesato come ref."""
    p = preds.dropna(subset=[f"pred_{var}", f"nwp_{var}", f"prim_{var}"]).copy()
    p["ae_nwp_prim"] = (p[f"nwp_{var}"] - p[f"prim_{var}"]).abs()
    p["ae_ml_prim"] = (p[f"pred_{var}"] - p[f"prim_{var}"]).abs()
    p["ae_nwp_wgt"] = (p[f"nwp_{var}"] - p[f"wgt_{var}"]).abs()
    p["ae_ml_wgt"] = (p[f"pred_{var}"] - p[f"wgt_{var}"]).abs()

    def agg(g: pd.DataFrame) -> pd.Series:
        mae_nwp_p, mae_ml_p = g["ae_nwp_prim"].mean(), g["ae_ml_prim"].mean()
        mae_nwp_w, mae_ml_w = g["ae_nwp_wgt"].mean(), g["ae_ml_wgt"].mean()
        return pd.Series({
            "n": len(g),
            "nwp_vs_prim": mae_nwp_p, "ml_vs_prim": mae_ml_p,
            "skill_prim": (1 - mae_ml_p / mae_nwp_p) * 100 if mae_nwp_p else float("nan"),
            "skill_wgt": (1 - mae_ml_w / mae_nwp_w) * 100 if mae_nwp_w else float("nan"),
        })

    rows = {str(loc): agg(g) for loc, g in p.groupby("location_id", observed=True)}
    rows["__ALL__"] = agg(p)
    return pd.DataFrame(rows).T


@app.command()
def main(
    db: Path = typer.Option(DEFAULT_DB, help="Path DuckDB"),
    config_dir: Path = typer.Option(REPO / "config", help="Dir config YAML"),
    n_splits: int = typer.Option(4, help="Numero di fold walk-forward"),
) -> None:
    stations = primary_stations(config_dir)
    con = duckdb.connect(str(db), read_only=True)
    df = _load_features(con)
    primary = _load_primary_obs(con, stations)
    con.close()

    typer.echo("Walk-forward (target di training = pesato; valutazione = staz. primaria):")
    preds = _walk_forward_predictions(df, n_splits=n_splits)
    preds = preds.merge(primary, on=["location_id", "target_date"], how="left")

    for var in ("tmin_c", "tmax_c"):
        tab = _skill_table(preds, var)
        typer.echo(f"\n### {var}  —  skill ML vs NWP-mean (out-of-sample, °C)")
        typer.echo(tab.to_string(float_format=lambda x: f"{x:.3f}"))
    typer.echo("\nskill_prim = skill contro gauge primario (claim difendibile per il case study)")
    typer.echo("skill_wgt  = skill contro target pesato (ref: deve riavvicinare il +32/+43% CV)")


if __name__ == "__main__":
    app()
