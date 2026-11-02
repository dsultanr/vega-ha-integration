"""Verify HA units, state classes and unavailable/zero semantics with real HA."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.vega.const import SENSOR_TYPES
from custom_components.vega.sensor import VegaSensor


class EntityTests(unittest.TestCase):
    def entity(self, key, data):
        coordinator = SimpleNamespace(imei='test', device_name='Vega test', entry_id='test',
                                      data=data, config_entry=None, last_update_success=True)
        return VegaSensor(coordinator, key, *SENSOR_TYPES[key])

    def test_native_units_and_classes(self):
        for key, unit, device_class in (('hv_battery_voltage', 'V', 'voltage'),
                                        ('hv_battery_current', 'A', 'current'),
                                        ('hv_battery_power', 'kW', 'power')):
            entity = self.entity(key, {key: 1.5})
            self.assertEqual(entity.native_unit_of_measurement, unit)
            self.assertEqual(entity.device_class, device_class)
            self.assertEqual(entity.state_class, 'measurement')
            self.assertEqual(entity.unique_id, 'vega_test_' + key)
            self.assertTrue(entity.available)

    def test_missing_inputs_and_expiry_are_unavailable(self):
        for key in ('hv_battery_voltage', 'hv_battery_current', 'hv_battery_power'):
            self.assertFalse(self.entity(key, {}).available)
            self.assertFalse(self.entity(key, {key: None}).available)

    def test_zero_power_is_available_and_negative_power_keeps_sign(self):
        for value in (0, -2.676, 0.941):
            entity = self.entity('hv_battery_power', {'hv_battery_power': value})
            self.assertTrue(entity.available)
            self.assertEqual(entity.native_value, value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
