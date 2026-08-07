"""Demo a video del correttore orario (D-024).

Genera un DuckDB temporaneo con dati sintetici (forecast NWP con curva diurna +
osservazioni realtime con bias di forma noto), allena il correttore orario e
patcheggia due JSON del frontend così da confrontare a schermo la curva baseline
(shape NWP ancorata) vs corretta (correttore attivo).

  - casa_campi    → hourly[] baseline (nessun correttore)
  - lavoro_cosimo → hourly[] corretta (correttore attivo)

I dati sintetici simulano uno sfasamento di 2h del ciclo diurno (il massimo locale
arriva 2h dopo quello NWP): è il segnale che il correttore è nato per correggere —
il ri-ancoraggio ai daily anchor ML non può assorbirlo (min/max restano gli stessi,
cambia solo la forma), quindi la differenza è visibile a schermo.

Uso:
    uv run python analysis/hourly_corrector_demo.py
    uv run python analysis/hourly_corrector_demo.py --days 120 --db /tmp/demo.duckdb
    uv run python analysis/hourly_corrector_demo.py --restore   # ripristina i JSON originali

Poi, per vedere il risultato:
    cd frontend && python3 -m http.server 8000
    apri http://localhost:8000 → confronta Casa Campi (baseline) vs Lav. Cosimo (corretta),
    vista "Guazza ML" dello stesso giorno.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from guazza.hourly_corrector import (
    CORRECTOR_FILENAME,
    build_delta_dataset,
    load_corrector,
    train_corrector,
)
from guazza.output import compute_hourly_profile
from guazza.storage import DuckDBClient

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DATA = REPO_ROOT / "frontend" / "data"
BACKUP_DIR = FRONTEND_DATA / ".demo_backup"

DEMO_LOCATIONS = ["casa_campi", "lavoro_cosimo"]
NWP_SOURCES = [
    "open_meteo_ecmwf_ifs",
    "open_meteo_icon_eu",
    "open_meteo_arome_france",
    "open_meteo_italia_meteo_arpae_icon_2i",
]
UTC_OFFSET_H = 2  # CEST: i timestamp DB sono UTC naive; le curve sono in ora locale

# Anchors ML daily fittizi per il giorno demo (livelli che il modello daily darebbe)
DEMO_ANCHORS: dict[str, float] = {
    "tmin_p50": 10.0,
    "tmax_p50": 26.0,
    "tmin_ci80_lo": 8.0,
    "tmin_ci80_hi": 12.0,
    "tmax_ci80_lo": 24.0,
    "tmax_ci80_hi": 28.0,
}


def _nwp_temp_c(local_hour: int) -> float:
    """Curva diurna sintetica: min ~7°C alle 03, max ~23°C alle 15."""
    return round(15.0 + 8.0 * np.sin(np.pi * (local_hour - 9) / 12.0), 1)


def _obs_temp_c(local_hour: int, rng: np.random.Generator) -> float:
    """Osservazioni con ritardo di fase di 2h sul ciclo diurno NWP.

    Il segnale da correggere è uno sfasamento (il massimo locale arriva 2h dopo
    quello previsto): il ri-ancoraggio min/max non può assorbirlo (min e max
    restano gli stessi, cambia la forma), quindi è il caso d'uso reale del
    correttore. Il residuo Δ ≈ nwp(h−2) − nwp(h) ha ampiezza fino a ~3°C.
    """
    return round(_nwp_temp_c(local_hour - 2) + float(rng.normal(0.0, 0.3)), 1)


def _seed_db(db: DuckDBClient, days: int, rng: np.random.Generator) -> date:
    """Popola forecasts (4 modelli) + observations realtime per i giorni richiesti.

    Returns:
        Ultima data seminata (il giorno demo).
    """
    start = date(2026, 5, 1)
    forecast_records: list[dict[str, Any]] = []
    obs_records: list[dict[str, Any]] = []

    for loc in DEMO_LOCATIONS:
        # station_id distinti per location: la PK di observations è
        # (source, station_id, ts, granularity) — niente location_id
        sir_sid = "TOS01001215" if loc == "casa_campi" else "TOS01001300"
        neta_sid = "NETA_CAMPI" if loc == "casa_campi" else "NETA_COSIMO"
        for d in range(days):
            day = start + timedelta(days=d)
            for h in range(24):
                temp = _nwp_temp_c(h)
                # ts_valid in UTC naive = ora locale - 2h (CEST), con rollover di giorno
                ts_valid = datetime(day.year, day.month, day.day, h) - timedelta(hours=UTC_OFFSET_H)
                for src in NWP_SOURCES:
                    forecast_records.append({
                        "source": src, "location_id": loc,
                        "ts_run": ts_valid - timedelta(hours=18),
                        "ts_valid": ts_valid, "lead_time_h": 24,
                        "temp_c": temp, "humidity_pct": 60.0,
                        "precip_mm": 0.0, "wind_speed_ms": 2.0, "weather_code": 0,
                    })
                # 4 campioni realtime per ora (2 SIR + 2 Netatmo), mediana ≈ shape sfasata
                for j, (sid, source) in enumerate([
                    (sir_sid, "sir_toscana"),
                    (sir_sid, "sir_toscana"),
                    (neta_sid, "netatmo"),
                    (neta_sid, "netatmo"),
                ]):
                    obs_records.append({
                        "source": source, "station_id": sid, "location_id": loc,
                        "ts": ts_valid + timedelta(minutes=15 * j),
                        "granularity": "realtime",
                        "temp_c": _obs_temp_c(h, rng),
                    })

    db.upsert_forecasts(forecast_records)
    db.upsert_sir_observations(obs_records)
    return start + timedelta(days=days - 1)


def _patch_json(loc: str, demo_date: date, hourly: list[dict[str, Any]], backup: bool) -> None:
    """Sostituisce hourly[] del primo giorno con la versione data, facendo backup."""
    path = FRONTEND_DATA / f"{loc}.json"
    if not path.exists():
        raise SystemExit(f"JSON mancante: {path} — rigenera prima i dati del frontend")
    if backup:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, BACKUP_DIR / f"{loc}.json")
    payload = json.loads(path.read_text())
    day = payload["days"][0]
    day["target_date"] = str(demo_date)
    day["hourly"] = hourly
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"patched {path} (target_date={day['target_date']})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="Path DB temporaneo (default: tmp)")
    parser.add_argument("--model-dir", type=Path, default=REPO_ROOT / "data" / "models" / "demo")
    parser.add_argument("--days", type=int, default=90, help="Giorni sintetici (>= 60)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--restore", action="store_true", help="Ripristina i JSON originali")
    args = parser.parse_args()

    if args.restore:
        for loc in DEMO_LOCATIONS:
            src = BACKUP_DIR / f"{loc}.json"
            if not src.exists():
                print(f"nessun backup per {loc} — salto")
                continue
            shutil.copy2(src, FRONTEND_DATA / f"{loc}.json")
            print(f"ripristinato {loc}.json")
        return

    if args.days < 60:
        raise SystemExit("--days deve essere >= 60 (MIN_DAYS_PER_LOCATION)")

    db_path = args.db or Path(tempfile.mkdtemp(prefix="guazza_demo_")) / "demo.duckdb"
    rng = np.random.default_rng(args.seed)

    with DuckDBClient(db_path=db_path) as db:
        db.init_schema()
        print(f"DB sintetico: {db_path}")
        demo_date = _seed_db(db, args.days, rng)

        # 1. Dataset Δ + training (stessa soglia di produzione: -15% RMSE su holdout)
        locations: dict[str, Any] = {loc: {} for loc in DEMO_LOCATIONS}
        dataset = build_delta_dataset(db, locations, min_days=args.days)
        if dataset is None:
            raise SystemExit("build_delta_dataset: dati insufficienti — controlla la semina")

        out_path = args.model_dir / CORRECTOR_FILENAME
        metrics = train_corrector(dataset, out_path)
        if metrics is None:
            raise SystemExit(
                "train_corrector: improvement sotto soglia -15% — il correttore non è stato salvato"
            )
        print(
            f"correttore salvato: {out_path} | rmse_base={metrics['rmse_base']} "
            f"rmse_model={metrics['rmse_model']} improvement={metrics['improvement_pct']}% "
            f"n_train={int(metrics['n_train'])} n_test={int(metrics['n_test'])}"
        )

        corrector = load_corrector(args.model_dir)

        # 2. Profilo orario del giorno demo: baseline vs corretta (stesso codice di prod)
        demo_str = str(demo_date)
        baseline = compute_hourly_profile(
            db, DEMO_LOCATIONS[0], demo_str,
            tmin_p50=DEMO_ANCHORS["tmin_p50"], tmax_p50=DEMO_ANCHORS["tmax_p50"],
            precip_anchor=0.0,
            tmin_ci80_lo=DEMO_ANCHORS["tmin_ci80_lo"], tmin_ci80_hi=DEMO_ANCHORS["tmin_ci80_hi"],
            tmax_ci80_lo=DEMO_ANCHORS["tmax_ci80_lo"], tmax_ci80_hi=DEMO_ANCHORS["tmax_ci80_hi"],
            precip_ci80_lo=0.0, precip_ci80_hi=0.0,
            corrector=None,
        )
        corrected = compute_hourly_profile(
            db, DEMO_LOCATIONS[1], demo_str,
            tmin_p50=DEMO_ANCHORS["tmin_p50"], tmax_p50=DEMO_ANCHORS["tmax_p50"],
            precip_anchor=0.0,
            tmin_ci80_lo=DEMO_ANCHORS["tmin_ci80_lo"], tmin_ci80_hi=DEMO_ANCHORS["tmin_ci80_hi"],
            tmax_ci80_lo=DEMO_ANCHORS["tmax_ci80_lo"], tmax_ci80_hi=DEMO_ANCHORS["tmax_ci80_hi"],
            precip_ci80_lo=0.0, precip_ci80_hi=0.0,
            corrector=corrector,
        )
        if baseline is None or corrected is None:
            raise SystemExit("compute_hourly_profile: nessun dato per il giorno demo")

        # 3. Patch dei JSON frontend (backup degli originali)
        _patch_json(DEMO_LOCATIONS[0], demo_date, baseline, backup=True)
        _patch_json(DEMO_LOCATIONS[1], demo_date, corrected, backup=True)

        # 4. Riepilogo delle differenze (h10/h22 = salita/discesa del ciclo diurno)
        t = lambda pts, h: pts[h]["temp_c"] or 0.0  # noqa: E731 — demo: ore sempre valorizzate
        diff_h10 = round(t(corrected, 10) - t(baseline, 10), 1)
        diff_h22 = round(t(corrected, 22) - t(baseline, 22), 1)
        print(f"\ndifferenza corretta−baseline: h10={diff_h10:+}°C (atteso ≈ −3, salita) "
              f"h22={diff_h22:+}°C (atteso ≈ +3, discesa)")

    print("\nApri il frontend:")
    print("  cd frontend && python3 -m http.server 8000")
    print("  http://localhost:8000 → giorno demo, vista 'Guazza ML'")
    print(f"  {DEMO_LOCATIONS[0]} = baseline · {DEMO_LOCATIONS[1]} = corretta")
    print("  Ripristino originali: uv run python analysis/hourly_corrector_demo.py --restore")


if __name__ == "__main__":
    main()
