"""Server entry point:  uvicorn app.main:app --reload

Environment variables:
    DECISION_CONFIG_DIR   folder of workflow JSON files   (default: ./configs)
    DECISION_DB_PATH      SQLite file                     (default: ./decisions.db)
    DECISION_ENABLE_SIMULATION   "0" removes the fault-injection endpoint (default: "1", for demos)
    DECISION_STALE_AFTER_SECONDS idle time before an active request counts as abandoned (default: 30)
"""
from __future__ import annotations

import os
from pathlib import Path

from app.api import create_app
from app.service import DecisionService

ROOT = Path(__file__).resolve().parent.parent

service = DecisionService(
    config_dir=os.environ.get("DECISION_CONFIG_DIR", ROOT / "configs"),
    db_path=os.environ.get("DECISION_DB_PATH", str(ROOT / "decisions.db")),
    stale_after_seconds=float(os.environ.get("DECISION_STALE_AFTER_SECONDS", "30")),
)
app = create_app(
    service,
    enable_simulation=os.environ.get("DECISION_ENABLE_SIMULATION", "1") != "0",
    recover_on_startup=True,   # finish anything a previous run left half-way
)
