"""Bounded, read-only sensor queries; response age is not CAN sample age."""
import asyncio
import math
import time

from . import protocol

DEFAULT_IDS = (2801, 2805, 2806, 2905, 2906, 2907, 2912, 2913, 2918, 2902, 2924, 2925)
ALLOWED_IDS = frozenset(DEFAULT_IDS) | frozenset(
    sid for sid, definition in protocol.SENSOR_DEFS.items()
    if definition.get("type") in ("bool", "int", "float")
)


def validate(sensor_ids, timeout):
    if not isinstance(sensor_ids, (list, tuple)) or not 1 <= len(sensor_ids) <= 16:
        raise ValueError("Specify 1..16 sensor IDs")
    if any(type(sid) is not int or sid not in ALLOWED_IDS for sid in sensor_ids):
        raise ValueError("Only known telemetry sensor IDs are allowed")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.5 <= timeout <= 6:
        raise ValueError("Timeout must be 0.5..6 seconds per sensor")
    return list(dict.fromkeys(sensor_ids))


async def read_sensors(session, sensor_ids=DEFAULT_IDS, timeout=2):
    ids = validate(sensor_ids, timeout)
    start = time.monotonic()
    report = {"started_at": time.time(), "source": "tracker_pull", "samples": [],
              "can_sample_time_known": False}
    for sid in ids:
        sample = {"id": sid, "requested_at": time.time()}
        before = time.monotonic()
        remaining = 30 - (before - start)
        if remaining <= 0:
            sample["error"] = "batch_deadline"
        else:
            try:
                limit = min(timeout, remaining)
                answer = await asyncio.wait_for(session.query_sensor(sid, 0, timeout=limit), limit)
                if answer is None:
                    sample["error"] = "no_reply"
                elif answer.get("id") != sid:
                    sample["error"] = "unexpected_sensor"
                else:
                    raw = bytes.fromhex(answer.get("raw_hex", ""))
                    definition = protocol.SENSOR_DEFS.get(sid, {})
                    width = 4 if definition.get("type") == "float" else definition.get("w", 1)
                    if len(raw) < width:
                        raise ValueError("Short sensor reply")
                    value = protocol.decode_sensor(sid, raw)["value"]
                    if value is None and "valid_range" in definition:
                        sample.update(error="sensor_unavailable", raw_hex=raw.hex(), received_at=time.time())
                        sample["elapsed_ms"] = round((time.monotonic() - before) * 1000, 1)
                        report["samples"].append(sample)
                        continue
                    if not isinstance(value, (bool, int, float)) or not math.isfinite(value):
                        raise ValueError("Non-finite or non-numeric reply")
                    sample.update(value=value, raw_hex=raw.hex(), received_at=time.time())
            except asyncio.TimeoutError:
                sample["error"] = "timeout"
            except Exception as err:
                # Do not expose transport error text, credentials or payloads.
                sample["error"] = type(err).__name__
        sample["elapsed_ms"] = round((time.monotonic() - before) * 1000, 1)
        report["samples"].append(sample)
    report["completed_at"] = time.time()
    report["complete"] = all("error" not in sample for sample in report["samples"])
    return report
