"""Export JSON Schemas from the executable contracts."""

import json
import argparse
from pathlib import Path

import g2g.models as models
from g2g.state import TrustedEvent

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = [
    getattr(models, name)
    for name in (
        "VisiblePacket",
        "AuditCase",
        "GoldRecord",
        "IdentityBinding",
        "History",
        "Request",
        "Observation",
        "BranchEvent",
    )
]
CONTRACTS.append(TrustedEvent)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "schemas")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for contract in CONTRACTS:
        (args.output / (contract.__name__ + ".schema.json")).write_text(
            json.dumps(contract.model_json_schema(), indent=2) + "\n", encoding="utf-8"
        )
