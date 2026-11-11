"""Shared fixtures.

Every test gets its own copy of the config folder and its own database file,
so tests can edit configs and inject faults without affecting each other.
Backoff sleeps are recorded instead of slept, so the suite runs instantly.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.dependencies import DependencyRegistry, MockCreditBureau, MockVendorRegistry
from app.service import DecisionService

CONFIGS = Path(__file__).resolve().parent.parent / "configs"


class Platform:
    """Everything a test needs to drive and inspect the system."""

    def __init__(self, tmp_path: Path):
        self.config_dir = tmp_path / "configs"
        shutil.copytree(CONFIGS, self.config_dir)
        self.db_path = str(tmp_path / "test.db")
        self.bureau = MockCreditBureau(scores={"A-GOOD": 780, "A-LOW": 610, "A-BORDER": 675})
        self.vendors = MockVendorRegistry(blacklist={"V-BAD"})
        self.sleeps: list[float] = []
        self.service = self.new_service()

    def new_service(self, stale_after_seconds: float = 0) -> DecisionService:
        """A fresh service over the same database: simulates a process restart.

        ``stale_after_seconds=0`` means an interrupted request can be resumed
        at once, so tests do not have to wait out the real 30 second window.
        """
        return DecisionService(
            self.config_dir, self.db_path,
            dependencies=DependencyRegistry([self.bureau, self.vendors]),
            sleep=self.sleeps.append,
            stale_after_seconds=stale_after_seconds,
        )

    def edit_config(self, filename: str, mutate) -> None:
        path = self.config_dir / filename
        config = json.loads(path.read_text(encoding="utf-8"))
        mutate(config)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def events(self, request_id: str, event_type: str) -> list[dict]:
        return [e for e in self.service.store.get_audit(request_id) if e["event_type"] == event_type]


@pytest.fixture
def platform(tmp_path) -> Platform:
    return Platform(tmp_path)


@pytest.fixture
def service(platform) -> DecisionService:
    return platform.service


@pytest.fixture
def loan() -> dict:
    """A loan application that passes every rule."""
    return {
        "applicant_id": "A-GOOD", "pan": "ABCDE1234F", "age": 30,
        "annual_income": 900000, "amount": 500000, "employment_type": "salaried",
    }


@pytest.fixture
def vendor() -> dict:
    """A vendor that passes every rule."""
    return {
        "vendor_id": "V-OK", "legal_name": "Acme Supplies Pvt Ltd", "category": "it_services",
        "annual_contract_value": 400000, "gst_number": "29ABCDE1234F1Z5", "years_in_business": 6,
    }
