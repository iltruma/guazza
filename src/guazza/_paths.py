"""Percorsi di default condivisi — unico punto di lettura delle env DB_PATH/CONFIG_DIR/OUTPUT_DIR.

Le variabili sono lette una volta a import time (stesso comportamento che avevano
i singoli moduli prima della centralizzazione). MODEL_DIR resta in models.py
perché ha un default locale diverso da quello del job predict.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DB_PATH = Path(os.environ.get("DB_PATH", "/var/lib/guazza/guazza.duckdb"))
DEFAULT_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", str(REPO_ROOT / "config")))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "data/output"))
