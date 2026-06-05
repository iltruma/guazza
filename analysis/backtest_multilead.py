"""Backtest multi-lead D+0…D+7 — skill ML vs NWP che degrada con l'orizzonte.

Misura come la skill del post-processing evolve dal nowcast (lead 0) al giorno+7,
usando i lead lunghi ricostruiti da `ingest multilead` (variabili previous_dayN,
archivio disponibile da ~nov 2025). Per evitare leakage il modello è addestrato sui
dati **precedenti** alla finestra multi-lead (cutoff con embargo) e valutato out-of-sample
su di essa, lead per lead, contro il target pesato e contro la stazione SIR primaria.

Solo la mediana q=0.50 (la MAE non richiede quantili/CQR). Read-only sul DB.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import typer
import yaml

from guazza.models import _TARGET_NWP_MEAN, FEATURE_COLS, TARGETS, _train_lgbm

app = typer.Typer(add_completion=False)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO / "data" / "guazza.duckdb"
LEADS = [0, 24, 48, 72, 96, 120, 144, 168]


def _primary_stations(config_dir: Path) -> dict[str, str]:
    data = yaml.safe_load((config_dir / "locations.yaml").read_text())
    return {loc_id: spec["sir_station_id"]
            for loc_id, spec in data["locations"].items()
            if spec.get("sir_station_id")}


def _load_features(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute("SELECT * FROM features_daily").df()
    df["location_id"] = df["location_id"].astype("category")
    df["target_date"] = pd.to_datetime(df["target_date"]).dt.date
    return df.sort_values("target_date").reset_index(drop=True)


def _load_primary_obs(con: duckdb.DuckDBPyConnection, stations: dict[str, str]) -> pd.DataFrame:
    station_map = ", ".join(f"('{loc}','{st}')" for loc, st in stations.items())
    df = con.execute(f"""
        WITH st(location_id, station_id) AS (VALUES {station_map})
        SELECT st.location_id, o.ts::date AS target_date,
               o.tmin_c AS prim_tmin_c, o.tmax_c AS prim_tmax_c
        FROM observations o JOIN st ON o.station_id = st.station_id
        WHERE o.source = 'sir_toscana' AND o.granularity = 'daily'
    """).df()
    df["target_date"] = pd.to_datetime(df["target_date"]).dt.date
    return df


@app.command()
def main(
    db: Path = typer.Option(DEFAULT_DB, help="Path DuckDB"),
    config_dir: Path = typer.Option(REPO / "config", help="Dir config YAML"),
    window_start: str = typer.Option("2025-10-15", help="Inizio finestra multi-lead (test)"),
    embargo_days: int = typer.Option(7, help="Giorni di embargo tra train e finestra"),
) -> None:
    stations = _primary_stations(config_dir)
    con = duckdb.connect(str(db), read_only=True)
    df = _load_features(con)
    primary = _load_primary_obs(con, stations)
    con.close()

    win_start = pd.to_datetime(window_start).date()
    cutoff = (pd.Timestamp(win_start) - pd.Timedelta(days=embargo_days)).date()

    # Train q=0.50 sui dati prima della finestra (tutti lead-0 nello storico).
    train = df[df["target_date"] <= cutoff]
    models = {}
    for target in TARGETS:
        col = f"target_{target}"
        mask = train[col].notna()
        models[target] = _train_lgbm(train.loc[mask, FEATURE_COLS], train.loc[mask, col], 0.50)
    typer.echo(f"Train: {len(train)} righe ≤ {cutoff} | Test: finestra ≥ {win_start}")

    test = df[df["target_date"] >= win_start].copy()
    # Predici prima del merge: la merge con primary altera il dtype categorical
    # di location_id e LightGBM rifiuta categorie non allineate al training.
    for target in TARGETS:
        test[f"pred_{target}"] = models[target].predict(test[FEATURE_COLS])
    test = test.merge(primary, on=["location_id", "target_date"], how="left")

    for var in ("tmin_c", "tmax_c"):
        nwp_col = _TARGET_NWP_MEAN[var]
        prim_col = f"prim_{var.replace('_c', '')}_c"  # prim_tmin_c / prim_tmax_c
        rows = []
        for lead in LEADS:
            g = test[test["lead_time_h"] == lead]
            # vs target pesato
            gp = g.dropna(subset=[f"pred_{var}", nwp_col, f"target_{var}"])
            mae_nwp = (gp[nwp_col] - gp[f"target_{var}"]).abs().mean()
            mae_ml = (gp[f"pred_{var}"] - gp[f"target_{var}"]).abs().mean()
            skill_w = (1 - mae_ml / mae_nwp) * 100 if mae_nwp else float("nan")
            # vs gauge primario
            gpr = g.dropna(subset=[f"pred_{var}", nwp_col, prim_col])
            mae_nwp_p = (gpr[nwp_col] - gpr[prim_col]).abs().mean()
            mae_ml_p = (gpr[f"pred_{var}"] - gpr[prim_col]).abs().mean()
            skill_p = (1 - mae_ml_p / mae_nwp_p) * 100 if mae_nwp_p else float("nan")
            rows.append({"lead_h": lead, "n": len(gp),
                         "mae_nwp": mae_nwp, "mae_ml": mae_ml, "skill_wgt_%": skill_w,
                         "skill_prim_%": skill_p})
        tab = pd.DataFrame(rows)
        typer.echo(f"\n### {var}  —  skill per lead (test {win_start}→oggi)")
        typer.echo(tab.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

    typer.echo("\nmae_* vs target pesato | skill_wgt = vs target | skill_prim = vs gauge SIR primario")


if __name__ == "__main__":
    app()
