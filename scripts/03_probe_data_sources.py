#!/usr/bin/env python3
"""Task 0 — Script 03: verifica accessibilità e formato delle sorgenti dati.

Per ogni sorgente, testa:
- Endpoint raggiungibile (HTTP status)
- robots.txt check
- Struttura risposta (JSON / HTML / GRIB)
- Copertura temporale (dove applicabile)

Output: report Markdown su stdout (redirigere su docs/task0_data_sources.md).

Uso:
    uv run python scripts/03_probe_data_sources.py > docs/task0_data_sources.md
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

SOURCES_YAML = Path(__file__).parent.parent / "config" / "sources.yaml"

# Coordinate di test: Campi Bisenzio
TEST_LAT = 43.825
TEST_LON = 11.140

TIMEOUT = 20


def robots_ok(base_url: str, path: str = "/") -> dict[str, Any]:
    """Controlla robots.txt per il path specificato."""
    try:
        # Estrai schema + host
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        r = httpx.get(robots_url, timeout=10, follow_redirects=True)
        content = r.text if r.status_code == 200 else ""
        # Controllo molto semplice: cerca "Disallow: /" o "Disallow: <path>"
        disallowed = any(
            f"Disallow: {path}" in line or "Disallow: /" == line.strip()
            for line in content.splitlines()
            if not line.startswith("#")
        )
        return {
            "robots_url": robots_url,
            "status": r.status_code,
            "disallowed": disallowed,
            "snippet": content[:500] if content else "(vuoto)",
        }
    except Exception as e:
        return {"error": str(e)}


def probe_open_meteo_historical() -> dict[str, Any]:
    """Verifica copertura Open-Meteo Historical Forecast API per le coordinate di test."""
    url = "https://historical-forecast-api.open-meteo.com/v1/forecast"
    # Prova con ECMWF: 3 anni fa
    from datetime import timedelta
    start = (datetime.now(UTC) - timedelta(days=3 * 365)).strftime("%Y-%m-%d")
    end = (datetime.now(UTC) - timedelta(days=3 * 365 - 3)).strftime("%Y-%m-%d")
    params = {
        "latitude": TEST_LAT,
        "longitude": TEST_LON,
        "start_date": start,
        "end_date": end,
        "hourly": "temperature_2m",
        "models": "ecmwf_ifs025",
    }
    try:
        r = httpx.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            n_values = len(data.get("hourly", {}).get("temperature_2m", []))
            return {
                "status": r.status_code,
                "ok": True,
                "test_start": start,
                "test_end": end,
                "n_hourly_values": n_values,
                "models_tested": ["ecmwf_ifs025"],
            }
        else:
            return {"status": r.status_code, "ok": False, "body": r.text[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def probe_cfr_realtime() -> dict[str, Any]:
    """Verifica pagina real-time CFR Toscana."""
    url = "http://www.cfr.toscana.it/index.php?IDS=10&IDT=9"
    try:
        r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "lxml")
        tables = soup.find_all("table")
        forms = soup.find_all("form")
        # Cerca elementi con dati meteo
        station_hints = []
        for el in soup.find_all(["td", "th"], string=True):
            text = el.get_text(strip=True)
            if any(kw in text.lower() for kw in ["temperatura", "stazione", "mm", "°c"]):
                station_hints.append(text[:80])
                if len(station_hints) >= 5:
                    break
        return {
            "status": r.status_code,
            "ok": r.status_code == 200,
            "n_tables": len(tables),
            "n_forms": len(forms),
            "station_hints": station_hints[:5],
            "content_length": len(r.text),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def probe_arpat() -> dict[str, Any]:
    """Verifica se ARPAT ha endpoint JSON o solo HTML."""
    results: dict[str, Any] = {}
    candidates = [
        "https://www.arpat.toscana.it/dati-e-indicatori/dati-in-tempo-reale/qualita-dell-aria",
        "https://www.arpat.toscana.it/api/qualita-aria",  # ipotetico
        "https://dati.toscana.it/api/3/action/package_search?q=qualita+aria+ARPAT",
    ]
    for url in candidates:
        try:
            r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
            ct = r.headers.get("content-type", "")
            results[url] = {
                "status": r.status_code,
                "content_type": ct,
                "is_json": "json" in ct,
                "size_bytes": len(r.content),
            }
        except Exception as e:
            results[url] = {"error": str(e)}
    return results


def probe_cfr_alerts() -> dict[str, Any]:
    """Verifica formato bollettini CFR Toscana."""
    candidates = [
        "http://www.cfr.toscana.it/index.php?IDS=10&IDT=76",
        "http://www.cfr.toscana.it/index.php?IDS=10&IDT=38",  # allerte
        "http://www.cfr.toscana.it/rss/",
    ]
    results: dict[str, Any] = {}
    for url in candidates:
        try:
            r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
            ct = r.headers.get("content-type", "")
            is_rss = "xml" in ct or r.text[:100].lstrip().startswith("<?xml")
            is_pdf = "pdf" in ct
            results[url] = {
                "status": r.status_code,
                "content_type": ct,
                "is_rss": is_rss,
                "is_pdf": is_pdf,
                "is_json": "json" in ct,
                "snippet": r.text[:200] if r.status_code == 200 else "",
            }
        except Exception as e:
            results[url] = {"error": str(e)}
    return results


def probe_prociv() -> dict[str, Any]:
    """Verifica API Protezione Civile allerte."""
    url = "https://mappe.protezionecivile.gov.it/ms/api/v2/allerte"
    try:
        r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
        ct = r.headers.get("content-type", "")
        result: dict[str, Any] = {
            "status": r.status_code,
            "content_type": ct,
            "is_json": "json" in ct,
        }
        if "json" in ct and r.status_code == 200:
            data = r.json()
            result["keys"] = list(data.keys()) if isinstance(data, dict) else f"array[{len(data)}]"
            # Cerca Toscana nei dati
            text = json.dumps(data)
            result["has_toscana"] = "Toscana" in text or "TOS" in text
        else:
            result["snippet"] = r.text[:300]
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def probe_lamma() -> dict[str, Any]:
    """Verifica accesso dati LAMMA Toscana."""
    candidates = [
        "http://www.lamma.toscana.it",
        "http://dati.lamma.toscana.it",
        "https://www.lamma.rete.toscana.it/meteo/modelli/wrf",
    ]
    results: dict[str, Any] = {}
    for url in candidates:
        try:
            r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
            results[url] = {
                "status": r.status_code,
                "content_type": r.headers.get("content-type", ""),
                "final_url": str(r.url),
            }
        except Exception as e:
            results[url] = {"error": str(e)}
    return results


def format_probe_result(name: str, result: dict[str, Any], ok_key: str = "ok") -> str:
    ok = result.get(ok_key, result.get("status") == 200)
    icon = "✅" if ok else "❌"
    return f"{icon} {name}"


def main() -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("# Task 0 — Probe sorgenti dati\n")
    print(f"Generato: {now}\n")
    print(f"Coordinate di test: lat={TEST_LAT}, lon={TEST_LON} (Campi Bisenzio)\n")
    print("---\n")

    print("## 1. Open-Meteo Historical Forecast API\n")
    print("  [probe] Open-Meteo Historical...", file=sys.stderr)
    r = probe_open_meteo_historical()
    if r.get("ok"):
        print("- **Status**: ✅ OK")
        print(f"- Test periodo: `{r.get('test_start')}` → `{r.get('test_end')}`")
        print(f"- Valori orari restituiti: `{r.get('n_hourly_values')}`")
        print(f"- Modelli testati: `{r.get('models_tested')}`")
        print("\n> **Conclusione**: storico disponibile per backfill Sprint 1 ✅")
    else:
        print("- **Status**: ❌ ERRORE")
        print(f"- Dettaglio: `{r.get('error') or r.get('body', '')[:200]}`")
        print("\n> **Conclusione**: endpoint non raggiungibile o copertura insufficiente ⚠️")
    print()

    print("## 2. CFR Toscana real-time\n")
    print("  [probe] CFR real-time...", file=sys.stderr)
    r = probe_cfr_realtime()
    robots = robots_ok("http://www.cfr.toscana.it", "/")
    if r.get("ok"):
        print(f"- **Status**: ✅ HTTP {r.get('status')}")
        print(f"- Tabelle HTML trovate: `{r.get('n_tables')}`")
        print(f"- Indizi dati meteo: `{r.get('station_hints')}`")
        print(f"- Dimensione pagina: `{r.get('content_length')} bytes`")
    else:
        print(f"- **Status**: ❌ Errore: `{r.get('error')}`")
    print(f"- robots.txt: `{robots.get('robots_url')}` — Disallow: `{robots.get('disallowed')}`")
    print()

    print("## 3. ARPAT qualità aria\n")
    print("  [probe] ARPAT...", file=sys.stderr)
    r = probe_arpat()
    has_json = any(v.get("is_json") for v in r.values() if isinstance(v, dict))
    print(f"- **JSON endpoint trovato**: {'✅ SÌ' if has_json else '❌ NO — solo HTML'}\n")
    for url, result in r.items():
        status = result.get("status", "ERR")
        ct = result.get("content_type", "?")
        err = result.get("error", "")
        icon = "✅" if result.get("is_json") else ("⚠️" if not err else "❌")
        print(f"  - {icon} `{url}`: HTTP {status}, `{ct}`")
    if not has_json:
        print("\n> **Conclusione**: ARPAT richiede scraping HTML. Selettori da identificare manualmente.")
    else:
        print("\n> **Conclusione**: endpoint JSON disponibile ✅")
    print()

    print("## 4. CFR Toscana bollettini allerte\n")
    print("  [probe] CFR allerte...", file=sys.stderr)
    r = probe_cfr_alerts()
    for url, result in r.items():
        status = result.get("status", "ERR")
        err = result.get("error", "")
        if not err:
            fmt = []
            if result.get("is_rss"):
                fmt.append("RSS/XML")
            if result.get("is_pdf"):
                fmt.append("PDF")
            if result.get("is_json"):
                fmt.append("JSON")
            if not fmt:
                fmt.append("HTML")
            icon = "✅" if not err and status == 200 else "❌"
            print(f"- {icon} `{url}`: HTTP {status}, formato: `{', '.join(fmt)}`")
            if result.get("snippet"):
                print(f"  ```\n  {result['snippet'][:150]}\n  ```")
        else:
            print(f"- ❌ `{url}`: errore `{err}`")
    print()

    print("## 5. Protezione Civile API allerte\n")
    print("  [probe] ProCiv...", file=sys.stderr)
    r = probe_prociv()
    if r.get("is_json") or r.get("status") == 200:
        print(f"- **Status**: ✅ HTTP {r.get('status')}")
        print(f"- JSON: `{r.get('is_json')}`")
        if r.get("keys"):
            print(f"- Chiavi risposta: `{r.get('keys')}`")
        print(f"- Contiene dati Toscana: `{r.get('has_toscana')}`")
    else:
        print(f"- **Status**: ❌ Errore: `{r.get('error') or r.get('snippet', '')[:200]}`")
    print()

    print("## 6. LAMMA Toscana (WRF GRIB)\n")
    print("  [probe] LAMMA...", file=sys.stderr)
    r = probe_lamma()
    for url, result in r.items():
        status = result.get("status", "ERR")
        err = result.get("error", "")
        final = result.get("final_url", url)
        icon = "✅" if status == 200 else ("⚠️" if not err else "❌")
        print(f"- {icon} `{url}`: HTTP {status}")
        if final != url:
            print(f"  Redirect → `{final}`")
        if err:
            print(f"  Errore: `{err}`")
    print("\n> **Nota**: accesso dati GRIB LAMMA da verificare manualmente su dati.lamma.toscana.it")
    print("> Non necessario per Sprint 1 (LAMMA è previsto per Sprint 2).\n")

    print("---\n")
    print("## Riepilogo e prossimi passi\n")
    print("| Sorgente | Status | Formato | Note |")
    print("|---|---|---|---|")
    print("| Open-Meteo Historical | DA COMPILARE | JSON REST | Endpoint principale Sprint 1 |")
    print("| CFR real-time | DA COMPILARE | HTML scraping | Fragile, tenacity required |")
    print("| ARPAT | DA COMPILARE | DA VERIFICARE | Endpoint JSON o scraping |")
    print("| CFR allerte | DA COMPILARE | DA VERIFICARE | RSS/PDF/HTML? |")
    print("| ProCiv | DA COMPILARE | DA VERIFICARE | API REST probabile |")
    print("| LAMMA GRIB | Sprint 2 | GRIB2 | Non urgente |")
    print()
    print("> Compilare la colonna 'Status' con i risultati di questo script.")
    print("> Aggiornare `config/sources.yaml` con gli status finali.")


if __name__ == "__main__":
    main()
