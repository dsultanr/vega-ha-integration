"""Traction-battery values, signed power, freshness and replay regressions."""
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'custom_components' / 'vega'))
import hv_battery as hv
import protocol


class BatteryTests(unittest.TestCase):
    def setUp(self):
        self.battery = hv.HvBatteryTelemetry()

    def put(self, key, value, timestamp=1000, source='vega', received=None):
        return self.battery.update(key, value, observed=timestamp,
                                   now=timestamp if received is None else received, source=source)

    def pair(self, volts=361.9, amps=2.6, timestamp=1000):
        self.put(hv.VOLTAGE, volts, timestamp)
        self.put(hv.CURRENT, amps, timestamp)

    def test_initial_state_is_unavailable(self):
        self.assertEqual(self.battery.snapshot(1000, True), dict.fromkeys(hv.OUTPUTS))

    def test_real_tracker_float32_padding_and_power(self):
        # Actual readback from the F515 tracker on 2026-11-02.
        for sid, raw, expected in ((2922, '33f3b44300000000', 361.9),
                                   (2923, '8066264000000000', 2.6)):
            sensor = protocol.decode_sensor(sid, bytes.fromhex(raw))
            self.assertEqual(sensor['value'], expected)
            self.put(sensor['name'], sensor['value'])
        self.assertEqual(self.battery.snapshot(1000, True)[hv.POWER], 0.941)

    def test_charge_and_regeneration_keep_negative_sign(self):
        sensor = protocol.decode_sensor(2923, struct.pack('<f', -7.2) + bytes(4))
        self.pair(371.6, sensor['value'])
        self.assertEqual(self.battery.snapshot(1000, True)[hv.POWER], -2.676)

    def test_zero_current_is_valid_zero_power(self):
        self.pair(382.1, 0.0)
        self.assertEqual(self.battery.snapshot(1000, True)[hv.POWER], 0)

    def test_zero_voltage_reset_invalidates_power(self):
        self.pair()
        self.put(hv.VOLTAGE, 0, 1001)
        result = self.battery.snapshot(1001, True)
        self.assertIsNone(result[hv.VOLTAGE])
        self.assertIsNone(result[hv.POWER])

    def test_single_input_never_fabricates_power(self):
        self.put(hv.VOLTAGE, 361.9)
        self.assertIsNone(self.battery.snapshot(1000, True)[hv.POWER])

    def test_missing_current_updates_expire_even_if_voltage_keeps_arriving(self):
        self.pair()
        self.put(hv.VOLTAGE, 362, 1090)
        result = self.battery.snapshot(1090, True)
        self.assertEqual(result[hv.VOLTAGE], 362)
        self.assertIsNone(result[hv.CURRENT])
        self.assertIsNone(result[hv.POWER])

    def test_age_boundary(self):
        self.pair()
        self.assertIsNotNone(self.battery.snapshot(1089.999, True)[hv.POWER])
        self.assertEqual(self.battery.snapshot(1090, True), dict.fromkeys(hv.OUTPUTS))

    def test_offline_values_are_unavailable(self):
        self.pair()
        self.assertEqual(self.battery.snapshot(1001, False), dict.fromkeys(hv.OUTPUTS))

    def test_new_voltage_does_not_multiply_old_current(self):
        self.pair()
        self.put(hv.VOLTAGE, 380, 1030)
        self.assertIsNone(self.battery.snapshot(1030, True)[hv.POWER])
        self.put(hv.CURRENT, 10, 1031)
        self.assertEqual(self.battery.snapshot(1031, True)[hv.POWER], 3.8)

    def test_different_transports_never_form_pair(self):
        self.put(hv.VOLTAGE, 360, source='combine')
        self.put(hv.CURRENT, 3, source='vega')
        self.assertIsNone(self.battery.snapshot(1000, True)[hv.POWER])

    def test_invalid_input_clears_previous_value(self):
        for bad in (None, '2.6', '00ab', True, False, float('nan'), float('inf'), -float('inf'), -401, 1238.3):
            with self.subTest(value=bad):
                self.setUp()
                self.pair()
                self.put(hv.CURRENT, bad, 1001)
                self.assertIsNone(self.battery.snapshot(1001, True)[hv.POWER])

    def test_truncated_binary_values_are_not_numbers(self):
        for raw in (b'', b'\x00', b'\x01\x02\x03'):
            self.setUp()
            sensor = protocol.decode_sensor(2923, raw)
            self.pair()
            self.put(hv.CURRENT, sensor['value'], 1001)
            self.assertIsNone(self.battery.snapshot(1001, True)[hv.POWER])

    def test_old_history_does_not_resurrect_readings(self):
        self.assertFalse(self.put(hv.VOLTAGE, 380, 900, 'combine', 1000))
        self.assertIsNone(self.battery.snapshot(1000, True)[hv.VOLTAGE])

    def test_replay_never_refreshes_expiry_or_rolls_back_latest(self):
        self.put(hv.VOLTAGE, 360, 1000, 'combine')
        self.assertFalse(self.put(hv.VOLTAGE, 370, 1000, 'combine', 1020))
        self.assertFalse(self.put(hv.VOLTAGE, 380, 999, 'combine', 1020))
        self.assertEqual(self.battery.snapshot(1020, True)[hv.VOLTAGE], 360)
        self.assertIsNone(self.battery.snapshot(1090, True)[hv.VOLTAGE])

    def test_missing_or_future_message_timestamp_is_not_live(self):
        for observed in (None, float('nan'), float('inf'), 1006):
            self.assertFalse(self.battery.update(hv.VOLTAGE, 360, now=1000,
                                                observed=observed, source='combine'))

    def test_delayed_message_expires_by_measurement_time(self):
        self.put(hv.VOLTAGE, 360, 1000, 'combine', 1080)
        self.assertEqual(self.battery.next_expiry(1080), 1090)
        self.assertIsNone(self.battery.snapshot(1090, True)[hv.VOLTAGE])

    def test_independent_expirations_are_scheduled(self):
        self.put(hv.VOLTAGE, 360, 1000)
        self.put(hv.CURRENT, 3, 1001)
        self.assertEqual(self.battery.next_expiry(1001), 1090)
        self.assertEqual(self.battery.next_expiry(1090), 1091)
        self.assertIsNone(self.battery.next_expiry(1091))


if __name__ == '__main__':
    unittest.main(verbosity=2)
