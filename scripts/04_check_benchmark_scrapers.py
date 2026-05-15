#!/usr/bin/env python3
"""Task 0 — Script 04: verifica scraper benchmark provider meteo commerciali.

Per ogni provider (Yr.no, MeteoAM, 3BMeteo, iLMeteo, MeteoGiuliacci):
- Verifica URL pattern parametrico per coordinate/comune
- Controlla robots.txt e ToS
- Testa selettori HTML attuali
- Nota dati estraibili (temperatura, precipitazione, etc.)

NOTA ETICA: scraping solo per benchmark statistico aggregato (MAE, CRPS).
Nessuna ridistribuzione di dati grezzi. Solo timestamp emissione + valore singolo
per confronto accuracy. Rispettare robots.txt.

Output: report Markdown su stdout (redirigere su docs/task0_scrapers.md).

Uso:
    uv run python scripts/04_check_benchmark_scrapers.py > docs/task0_scrapers.md
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

LOCATIONS_YAML = Path(__file__).parent.parent / "config" / "locations.yaml"

# Parametri test: Campi Bisenzio
TEST_LAT = 43.825
TEST_LON = 11.140
TEST_COMUNE = "campi-bisenzio"
TEST_COMUNE_3B = "campi+bisenzio"
TEST_ISTAT = "048007"   # codice ISTAT Campi Bisenzio (FI)

TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Guazza/0.1 meteo benchmark research "
        "(academic, aggregated stats only, no redistribution) "
        "contact: see github.com/guazza"
    )
}


def get_robots_txt(base_url: str) -> dict[str, Any]:
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        r = httpx.get(robots_url, timeout=10, headers=HEADERS, follow_redirects=True)
        return {
            "url": robots_url,
            "status": r.status_code,
            "content": r.text[:2000] if r.status_code == 200 else "",
            "error": None,
        }
    except Exception as e:
        return {"url": robots_url, "status": None, "content": "", "error": str(e)}


def probe_yr_no() -> dict[str, Any]:
    """Yr.no — API ufficiale MET Norway (preferita rispetto a scraping)."""
    url = (
        f"https://api.met.no/weatherapi/locationforecast/2.0/compact"
        f"?lat={TEST_LAT}&lon={TEST_LON}&altitude=35"
    )
    try:
        r = httpx.get(url, timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)
        result: dict[str, Any] = {
            "url": url,
            "status": r.status_code,
            "ok": r.status_code == 200,
        }
        if r.status_code == 200:
            data = r.json()
            timeseries = data.get("properties", {}).get("timeseries", [])
            result["n_timeseries"] = len(timeseries)
            if timeseries:
                first = timeseries[0]
                result["first_ts"] = first.get("time")
                instant = first.get("data", {}).get("instant", {}).get("details", {})
                result["available_variables"] = list(instant.keys())
        else:
            result["body"] = r.text[:500]
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def probe_meteoam() -> dict[str, Any]:
    """MeteoAM — Aeronautica Militare."""
    # Prova diversi URL possibili
    candidates = [
        f"https://www.meteoam.it/ta/query/meteoPoint?lat={TEST_LAT}&lon={TEST_LON}",
        f"https://www.meteoam.it/previsione-meteo/dettaglio/{TEST_ISTAT}",
        "https://www.meteoam.it",
    ]
    results: list[dict[str, Any]] = []
    for url in candidates:
        try:
            r = httpx.get(url, timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)
            ct = r.headers.get("content-type", "")
            entry: dict[str, Any] = {
                "url": url,
                "status": r.status_code,
                "content_type": ct,
                "is_json": "json" in ct,
            }
            if "json" in ct and r.status_code == 200:
                try:
                    data = r.json()
                    entry["json_keys"] = list(data.keys()) if isinstance(data, dict) else f"array[{len(data)}]"
                except Exception:
                    pass
            elif r.status_code == 200:
                soup = BeautifulSoup(r.text, "lxml")
                # Cerca elementi con temperature o previsioni
                temp_hints = []
                for el in soup.find_all(string=True):
                    text = str(el).strip()
                    if any(kw in text.lower() for kw in ["°c", "temperatura", "previsione", "mm"]):
                        temp_hints.append(text[:60])
                        if len(temp_hints) >= 3:
                            break
                entry["temp_hints"] = temp_hints
            results.append(entry)
        except Exception as e:
            results.append({"url": url, "error": str(e)})
    return {"candidates": results}


def probe_3bmeteo() -> dict[str, Any]:
    """3BMeteo — scraping HTML (solo benchmark aggregato)."""
    url = f"https://www.3bmeteo.com/meteo/{TEST_COMUNE_3B}"
    try:
        r = httpx.get(url, timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)
        soup = BeautifulSoup(r.text, "lxml")

        # Cerca selettori con temperatura
        temp_selectors_found = []
        for sel in [
            "span.tempMax", "span.tempMin", ".temp", "[class*='temp']",
            ".weather-temp", ".forecast-temp", "span[itemprop='temperature']",
        ]:
            els = soup.select(sel)
            if els:
                temp_selectors_found.append({
                    "selector": sel,
                    "count": len(els),
                    "sample": els[0].get_text(strip=True)[:30],
                })

        # Cerca script con JSON-LD o dati embedded
        scripts = soup.find_all("script", type="application/ld+json")
        has_json_ld = len(scripts) > 0

        return {
            "url": str(r.url),
            "status": r.status_code,
            "ok": r.status_code == 200,
            "temp_selectors_found": temp_selectors_found,
            "has_json_ld": has_json_ld,
            "page_title": soup.title.string if soup.title else "?",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def probe_ilmeteo() -> dict[str, Any]:
    """iLMeteo — scraping HTML (solo benchmark aggregato)."""
    url = f"https://www.ilmeteo.it/meteo/{TEST_COMUNE}"
    try:
        r = httpx.get(url, timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)
        soup = BeautifulSoup(r.text, "lxml")

        temp_selectors_found = []
        for sel in [
            ".tempMax", ".tempMin", ".temperature", "[class*='temp']",
            "span[itemprop='temperature']", ".weather__temperature",
            ".forecast__temperature",
        ]:
            els = soup.select(sel)
            if els:
                temp_selectors_found.append({
                    "selector": sel,
                    "count": len(els),
                    "sample": els[0].get_text(strip=True)[:30],
                })

        return {
            "url": str(r.url),
            "status": r.status_code,
            "ok": r.status_code == 200,
            "temp_selectors_found": temp_selectors_found,
            "page_title": soup.title.string if soup.title else "?",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def probe_meteogiuliacci() -> dict[str, Any]:
    """MeteoGiuliacci — scraping HTML (solo benchmark aggregato)."""
    url = f"https://www.meteogiuliacci.it/previsioni-meteo/{TEST_COMUNE}"
    try:
        r = httpx.get(url, timeout=TIMEOUT, headers=HEADERS, follow_redirects=True)
        soup = BeautifulSoup(r.text, "lxml")
        temp_selectors_found = []
        for sel in ["[class*='temp']", ".temperature", ".max-temp", ".min-temp"]:
            els = soup.select(sel)
            if els:
                temp_selectors_found.append({
                    "selector": sel,
                    "count": len(els),
                    "sample": els[0].get_text(strip=True)[:30],
                })
        return {
            "url": str(r.url),
            "status": r.status_code,
            "ok": r.status_code == 200,
            "temp_selectors_found": temp_selectors_found,
            "page_title": soup.title.string if soup.title else "?",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def print_robots_section(name: str, base_url: str) -> None:
    robots = get_robots_txt(base_url)
    print(f"**robots.txt** (`{robots['url']}`):")
    if robots.get("error"):
        print(f"- ❌ Errore: `{robots['error']}`")
    elif robots.get("status") != 200:
        print(f"- ⚠️ HTTP {robots.get('status')} (nessun robots.txt — scraping probabilmente ok)")
    else:
        content = robots.get("content", "")
        disallow_lines = [
            line for line in content.splitlines()
            if line.startswith("Disallow:") and ("*" in line or line.strip() == "Disallow: /")
        ]
        if disallow_lines:
            print(f"- ⚠️ Disallow globali trovati: `{disallow_lines[:3]}`")
            print("- **Verificare manualmente ToS prima di fare scraping**")
        else:
            print("- ✅ Nessun disallow globale trovato")
    print()


def main() -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("# Task 0 — Verifica scraper benchmark provider meteo\n")
    print(f"Generato: {now}\n")
    print(
        "> **Nota etica**: questi scraper sono usati SOLO per benchmark statistico aggregato\n"
        "> (MAE, CRPS vs osservazioni). Nessun dato grezzo ridistribuito.\n"
        "> Rispettiamo robots.txt e ToS di ogni sito.\n"
    )
    print(f"Parametri test: lat={TEST_LAT}, lon={TEST_LON}, comune=`{TEST_COMUNE}`\n")
    print("---\n")

    # 1. Yr.no (API ufficiale — preferita)
    print("## 1. Yr.no (API ufficiale MET Norway)\n")
    print("  [probe] Yr.no API...", file=sys.stderr)
    r = probe_yr_no()
    if r.get("ok"):
        print("- **Status**: ✅ HTTP 200 — API JSON ufficiale funzionante")
        print(f"- Timeseries disponibili: `{r.get('n_timeseries')}`")
        print(f"- Primo timestamp: `{r.get('first_ts')}`")
        print(f"- Variabili disponibili: `{r.get('available_variables')}`")
        print("\n> **Conclusione**: Yr.no usa API JSON ufficiale ✅")
        print("> Nessuno scraping necessario. Aggiungere header `User-Agent` identificativo.")
    else:
        print(f"- **Status**: ❌ Errore: `{r.get('error') or r.get('body', '')[:200]}`")
    print_robots_section("Yr.no", "https://api.met.no")
    print()

    # 2. MeteoAM
    print("## 2. MeteoAM — Aeronautica Militare\n")
    print("  [probe] MeteoAM...", file=sys.stderr)
    r = probe_meteoam()
    for c in r.get("candidates", []):
        status = c.get("status", "ERR")
        err = c.get("error", "")
        icon = "✅" if c.get("is_json") else ("⚠️" if status == 200 else "❌")
        print(f"- {icon} `{c['url']}`: HTTP {status}", end="")
        if c.get("is_json"):
            print(f" — JSON, chiavi: `{c.get('json_keys')}`")
        elif c.get("temp_hints"):
            print(f" — HTML, indizi temp: `{c.get('temp_hints')}`")
        elif err:
            print(f" — Errore: `{err}`")
        else:
            print()
    print_robots_section("MeteoAM", "https://www.meteoam.it")

    # 3. 3BMeteo
    print("## 3. 3BMeteo\n")
    print("  [probe] 3BMeteo...", file=sys.stderr)
    r = probe_3bmeteo()
    if r.get("ok"):
        print(f"- **Status**: ✅ HTTP {r.get('status')}")
        print(f"- Titolo pagina: `{r.get('page_title')}`")
        print(f"- Selettori temperatura trovati: {len(r.get('temp_selectors_found', []))}")
        for sel in r.get("temp_selectors_found", []):
            print(f"  - `{sel['selector']}` ({sel['count']} elementi): `{sel['sample']}`")
        print(f"- JSON-LD: `{r.get('has_json_ld')}`")
    else:
        print(f"- **Status**: ❌ Errore: `{r.get('error')}`")
    print_robots_section("3BMeteo", "https://www.3bmeteo.com")

    # 4. iLMeteo
    print("## 4. iLMeteo\n")
    print("  [probe] iLMeteo...", file=sys.stderr)
    r = probe_ilmeteo()
    if r.get("ok"):
        print(f"- **Status**: ✅ HTTP {r.get('status')}")
        print(f"- Titolo pagina: `{r.get('page_title')}`")
        print(f"- Selettori temperatura trovati: {len(r.get('temp_selectors_found', []))}")
        for sel in r.get("temp_selectors_found", []):
            print(f"  - `{sel['selector']}` ({sel['count']} elementi): `{sel['sample']}`")
    else:
        print(f"- **Status**: ❌ Errore: `{r.get('error')}`")
    print_robots_section("iLMeteo", "https://www.ilmeteo.it")

    # 5. MeteoGiuliacci
    print("## 5. MeteoGiuliacci\n")
    print("  [probe] MeteoGiuliacci...", file=sys.stderr)
    r = probe_meteogiuliacci()
    if r.get("ok"):
        print(f"- **Status**: ✅ HTTP {r.get('status')}")
        print(f"- Titolo pagina: `{r.get('page_title')}`")
        print(f"- Selettori temperatura trovati: {len(r.get('temp_selectors_found', []))}")
        for sel in r.get("temp_selectors_found", []):
            print(f"  - `{sel['selector']}` ({sel['count']} elementi): `{sel['sample']}`")
    else:
        print(f"- **Status**: ❌ Errore: `{r.get('error')}`")
    print_robots_section("MeteoGiuliacci", "https://www.meteogiuliacci.it")

    print("---\n")
    print("## Riepilogo priorità\n")
    print("| Provider | Metodo | Priorità | Note |")
    print("|---|---|---|---|")
    print("| Yr.no | API JSON ufficiale ✅ | Alta — Sprint 4 | Nessuno scraping, ToS chiaro |")
    print("| MeteoAM | DA VERIFICARE | Media — Sprint 4 | Verificare endpoint API |")
    print("| 3BMeteo | HTML scraping | Media — Sprint 4 | Selettori da aggiornare |")
    print("| iLMeteo | HTML scraping | Bassa — Sprint 4 | Selettori da aggiornare |")
    print("| MeteoGiuliacci | HTML scraping | Bassa — Sprint 4 | Selettori da aggiornare |")
    print()
    print("> Compilare questa tabella con i risultati effettivi del probe.\n")
    print("> Aggiornare `config/sources.yaml` con lo status finale di ogni provider.")


if __name__ == "__main__":
    main()
