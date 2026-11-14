#!/usr/bin/env python3
"""Produce a reversible climate telemetry plan from a full JSON snapshot.

Usage: python3 tools/prepare_climate_sensors.py snapshot.json > plan.json
Input: {"snapshot": {"5500/0": "...", ...}} or the inner mapping.
No tracker connection or writes. The writer must re-read the snapshot before
applying, compare each original immediately before writing, and verify readback.
"""
import argparse
import importlib
import json
from pathlib import Path
import sys
import types


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    # Load the pure planner without importing Home Assistant's __init__ module.
    pkg = types.ModuleType("vega_climate_plan")
    pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/vega")]
    sys.modules[pkg.__name__] = pkg
    profile = importlib.import_module(pkg.__name__ + ".climate_profile")
    data = json.loads(args.snapshot.read_text())
    raw = data.get("snapshot", data)
    snapshot = {key: bytes.fromhex(value) for key, value in raw.items()}
    changes = profile.prepare(snapshot)
    print(json.dumps({"profile": "f515_climate", "sensor_ids": profile.SENSOR_IDS,
                      "period_seconds": 5, "can_timeout_seconds": 15,
                      "changes": [{"key": key, "before": snapshot[key].hex(),
                                   "after": value.hex()} for key, value in changes.items()]}, indent=2))


if __name__ == "__main__":
    main()
