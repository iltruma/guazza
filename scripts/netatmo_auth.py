#!/usr/bin/env python3
"""
Setup OAuth2 Netatmo — da eseguire UNA VOLTA sola.

Avvia un server locale su :9999, apre l'URL di autorizzazione,
cattura il code dal redirect e salva access_token + refresh_token nel .env.

Prerequisito: aggiungere http://localhost:9999 come Redirect URI
nell'app Netatmo su https://dev.netatmo.com/apps

Uso:
    uv run python scripts/netatmo_auth.py
"""

import http.server
import os
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
ENV_FILE = ROOT / ".env"
PORT = 9999
REDIRECT_URI = f"http://localhost:{PORT}"
SCOPE = "read_station"
AUTH_URL = "https://api.netatmo.com/oauth2/authorize"
TOKEN_URL = "https://api.netatmo.com/oauth2/token"


def load_env() -> dict:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def update_env(updates: dict) -> None:
    """Aggiorna o aggiunge chiavi nel .env senza toccare le altre."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    existing_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                new_lines.append(f"{k}={updates[k]}")
                existing_keys.add(k)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in existing_keys:
            new_lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n")
    print(f"  .env aggiornato: {list(updates.keys())}")


def exchange_code(code: str, client_id: str, client_secret: str) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main():
    env = load_env()
    client_id = env.get("NETATMO_CLIENT_ID")
    client_secret = env.get("NETATMO_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("ERRORE: NETATMO_CLIENT_ID e NETATMO_CLIENT_SECRET mancanti nel .env")
        sys.exit(1)

    # Se già abbiamo un refresh_token, verifica che funzioni
    if env.get("NETATMO_REFRESH_TOKEN"):
        print("refresh_token già presente nel .env — verifica validità...")
        try:
            token = refresh_access_token(
                env["NETATMO_REFRESH_TOKEN"], client_id, client_secret
            )
            update_env({
                "NETATMO_ACCESS_TOKEN": token["access_token"],
                "NETATMO_REFRESH_TOKEN": token["refresh_token"],
            })
            print("Token rinnovato con successo.")
            return
        except Exception as e:
            print(f"refresh_token non valido ({e}), riautenticazione...")

    # Flusso authorization_code
    code_holder = {"code": None, "error": None}
    server_done = threading.Event()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                code_holder["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h2>Autorizzazione completata! Puoi chiudere questa scheda.</h2>")
            elif "error" in params:
                code_holder["error"] = params.get("error_description", ["errore sconosciuto"])[0]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h2>Errore di autorizzazione.</h2>")
            else:
                self.send_response(400)
                self.end_headers()
            server_done.set()

        def log_message(self, *args):
            pass  # silenzia i log HTTP

    # HTTPServer fa bind()+listen() in __init__, quindi il socket è già attivo.
    server = http.server.HTTPServer(("localhost", PORT), CallbackHandler)
    server_thread = threading.Thread(target=server.handle_request)
    server_thread.daemon = True
    server_thread.start()

    auth_params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": "guazza",
    })
    full_url = f"{AUTH_URL}?{auth_params}"

    print(f"\nAprendo il browser per autorizzare Netatmo...")
    print(f"URL (copia se il browser non si apre):\n  {full_url}\n")
    webbrowser.open(full_url)

    print("In attesa del callback su http://localhost:9999 ...")
    server_done.wait(timeout=120)

    if code_holder["error"]:
        print(f"ERRORE: {code_holder['error']}")
        sys.exit(1)
    if not code_holder["code"]:
        print("TIMEOUT: nessun codice ricevuto entro 120 secondi.")
        sys.exit(1)

    print("Code ricevuto, scambio per token...")
    try:
        token = exchange_code(code_holder["code"], client_id, client_secret)
    except Exception as e:
        print(f"ERRORE scambio code: {e}")
        sys.exit(1)

    update_env({
        "NETATMO_ACCESS_TOKEN": token["access_token"],
        "NETATMO_REFRESH_TOKEN": token["refresh_token"],
    })
    print("\nSetup completato. Token salvati nel .env.")
    print("Esegui questo script periodicamente per rinnovare il refresh_token.")


def refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


if __name__ == "__main__":
    main()
