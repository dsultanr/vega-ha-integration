"""Fresh, signed traction-battery measurements from the configured Vega sensors.

The tracker scales CAN data to volts/amps. Only multiply a recent pair from the
same transport; replayed Combine history must never become instantaneous power.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

VOLTAGE = "hv_battery_voltage"
CURRENT = "hv_battery_current"
POWER = "hv_battery_power"
INPUTS = (VOLTAGE, CURRENT)
OUTPUTS = (*INPUTS, POWER)
MAX_AGE = 90.0
MAX_PAIR_SKEW = 5.0
MAX_FUTURE_SKEW = 5.0


@dataclass(frozen=True)
class _Sample:
    value: float | None
    observed: float
    received: float
    source: str


class HvBatteryTelemetry:
    """Keep input timestamps independently of unrelated tracker keep-alives."""

    def __init__(self) -> None:
        self._samples: dict[str, _Sample] = {}

    def update(self, key: str, value: object, *, now: float,
               observed: float | None, source: str) -> bool:
        if key not in INPUTS:
            return False
        # Combine's own measurement timestamp is mandatory, including for a
        # backlog uploaded after reconnection. Direct replies are sampled now.
        if observed is None or not math.isfinite(observed):
            return False
        if not -MAX_FUTURE_SKEW <= now - observed < MAX_AGE:
            return False
        previous = self._samples.get(key)
        if previous and observed <= previous.observed:
            return False  # duplicate/reordered history cannot refresh expiry
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if valid:
            # 14-bit CAN fields; the zero-voltage reset is not a real reading.
            valid = 0 < value < 1638.3 if key == VOLTAGE else -400 <= value < 1238.3
        number = round(float(value), 2) if valid else None
        self._samples[key] = _Sample(number, observed, now, source)
        return True

    def _fresh(self, sample: _Sample, now: float) -> bool:
        return (sample.value is not None
                and -MAX_FUTURE_SKEW <= now - sample.observed < MAX_AGE
                and 0 <= now - sample.received < MAX_AGE)

    def snapshot(self, now: float, online: bool) -> dict[str, float | None]:
        result = dict.fromkeys(OUTPUTS)
        if not online:
            return result
        for key, sample in self._samples.items():
            if self._fresh(sample, now):
                result[key] = sample.value
        if all(result[key] is not None for key in INPUTS):
            voltage, current = (self._samples[key] for key in INPUTS)
            if (voltage.source == current.source
                    and abs(voltage.observed - current.observed) <= MAX_PAIR_SKEW):
                result[POWER] = round(voltage.value * current.value / 1000, 3)
        return result

    def next_expiry(self, now: float) -> float | None:
        expiries = [min(s.observed, s.received) + MAX_AGE for s in self._samples.values()
                    if self._fresh(s, now)]
        return min(expiries) if expiries else None
