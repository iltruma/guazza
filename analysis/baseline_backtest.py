"""Baseline backtest D+0 — de-risking della tesi di post-processing.

Confronta il forecast NWP grezzo (lead 0-5h, l'orizzonte piu' corto e quindi piu'
accurato) con la ground truth SIR, per capire se esiste un bias di microclima
sistematico e correggibile gia' a D+0. Se c'e' qui, e' il pavimento (floor) di skill
che a lead piu' lunghi puo' solo crescere; se non c'e' nemmeno qui, la tesi e' debole.

NB: lo storico nel DB contiene solo lead 0-5h. Il backtest multi-giorno (D+1..D+7)
non e' eseguibile finche' non si ri-ingesta l'orizzonte completo per run (punto aperto).

Read-only sul DB. Nessuna scrittura DuckDB.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import typer

from guazza.weights import primary_stations

app = typer.Typer(add_completion=False)

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO / "data" / "guazza.duckdb"
OUT_DIR = REPO / "analysis" / "out"

SEASON = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
          6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


def _load_daily(con: duckdb.DuckDBPyConnection, stations: dict[str, str]) -> pd.DataFrame:
    """Tabella tidy daily: (location, date, source, fc/obs tmin/tmax/precip).

    Forecast lead 0-5h aggregato sul giorno civile Europe/Rome (la tmin notturna
    cade a cavallo di mezzanotte UTC: aggregare su giorno UTC la disallinea dalla
    tmin SIR, calcolata sul giorno locale).
    """
    con.execute("INSTALL icu; LOAD icu;")
    station_map = ", ".join(f"('{loc}','{st}')" for loc, st in stations.items())

    return con.execute(f"""
        WITH stations(location_id, station_id) AS (VALUES {station_map}),
        fc_hourly AS (
            SELECT source, location_id,
                   ((ts_valid AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Rome')::date AS d,
                   temp_c, precip_mm
            FROM forecasts
            WHERE lead_time_h >= 0 AND lead_time_h < 6
        ),
        fc AS (
            SELECT source, location_id, d,
                   min(temp_c) AS fc_tmin, max(temp_c) AS fc_tmax,
                   sum(precip_mm) AS fc_precip
            FROM fc_hourly GROUP BY 1, 2, 3
        ),
        obs AS (
            SELECT s.location_id, o.ts::date AS d,
                   o.tmin_c AS obs_tmin, o.tmax_c AS obs_tmax, o.precip_mm AS obs_precip
            FROM observations o
            JOIN stations s ON o.station_id = s.station_id
            WHERE o.source = 'sir_toscana' AND o.granularity = 'daily'
        )
        SELECT fc.location_id, fc.d AS date, fc.source,
               fc.fc_tmin, fc.fc_tmax, fc.fc_precip,
               obs.obs_tmin, obs.obs_tmax, obs.obs_precip
        FROM fc JOIN obs USING (location_id, d)
        ORDER BY 1, 2, 3
    """).fetchdf()


def _add_multimodel(df: pd.DataFrame) -> pd.DataFrame:
    """Aggiunge la media multimodello come ulteriore 'source'."""
    mm = (df.groupby(["location_id", "date"], as_index=False)
            .agg(fc_tmin=("fc_tmin", "mean"), fc_tmax=("fc_tmax", "mean"),
                 fc_precip=("fc_precip", "mean"),
                 obs_tmin=("obs_tmin", "first"), obs_tmax=("obs_tmax", "first"),
                 obs_precip=("obs_precip", "first")))
    mm["source"] = "MULTIMODEL"
    return pd.concat([df, mm], ignore_index=True)


def _debias_metrics(df: pd.DataFrame, var: str, test_year: int) -> pd.DataFrame:
    """Per (location, source): MAE grezzo vs MAE debiasato sull'anno test.

    Bias mensile stimato sugli anni < test_year (disgiunti -> niente leakage),
    applicato all'anno test. Cattura il ciclo stagionale del bias di microclima.
    """
    fc_col, obs_col = f"fc_{var}", f"obs_{var}"
    d = df.dropna(subset=[fc_col, obs_col]).copy()
    d["year"] = d["date"].dt.year
    d["month"] = d["date"].dt.month
    d["err"] = d[fc_col] - d[obs_col]

    train = d[d["year"] < test_year]
    test = d[d["year"] == test_year]
    bias = (train.groupby(["location_id", "source", "month"])["err"]
                 .mean().rename("bias").reset_index())

    test = test.merge(bias, on=["location_id", "source", "month"], how="left")
    test["bias"] = test["bias"].fillna(0.0)
    test["ae_raw"] = test["err"].abs()
    test["ae_deb"] = (test["err"] - test["bias"]).abs()

    out = (test.groupby(["location_id", "source"])
               .agg(n=("ae_raw", "size"),
                    bias_mean=("err", "mean"),
                    mae_raw=("ae_raw", "mean"),
                    mae_deb=("ae_deb", "mean"))
               .reset_index())
    out["impr_pct"] = (1 - out["mae_deb"] / out["mae_raw"]) * 100
    return out.sort_values(["location_id", "mae_deb"])


@app.command()
def main(
    db: Path = typer.Option(DEFAULT_DB, help="Path DuckDB"),
    config_dir: Path = typer.Option(REPO / "config", help="Dir config YAML"),
    test_year: int = typer.Option(2025, help="Anno di test (train = anni precedenti)"),
) -> None:
    stations = primary_stations(config_dir)
    con = duckdb.connect(str(db), read_only=True)
    df = _add_multimodel(_load_daily(con, stations))
    con.close()
    df["date"] = pd.to_datetime(df["date"])

    print(f"\n{'='*72}\nBaseline backtest D+0 (lead 0-5h)  |  test_year={test_year}")
    print(f"Coperture: {df['date'].min().date()} -> {df['date'].max().date()}, "
          f"{df['location_id'].nunique()} location, {df['source'].nunique()-1} modelli + multimodello")
    print(f"Giorni con obs tmin: {df[df['source']=='MULTIMODEL']['obs_tmin'].notna().sum()}\n{'='*72}")

    for var in ("tmin", "tmax"):
        res = _debias_metrics(df, var, test_year)
        print(f"\n### {var.upper()}  —  MAE grezzo vs debiasato (anno {test_year}, °C)")
        show = res.rename(columns={"location_id": "location"})
        print(show.to_string(index=False,
              formatters={"bias_mean": "{:+.2f}".format, "mae_raw": "{:.2f}".format,
                          "mae_deb": "{:.2f}".format, "impr_pct": "{:+.0f}%".format}))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"baseline_d0_{test_year}.parquet"
    df.to_parquet(out_path)
    print(f"\nDataset tidy salvato: {out_path}")


if __name__ == "__main__":
    app()
