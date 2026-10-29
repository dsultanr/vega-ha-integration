"""Constants for the Vega integration."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

DOMAIN = "vega"

# Bundled hold-to-confirm Lovelace card (independent of the evolute integration).
# Bump CARD_VERSION together with the JS so the cache-buster forces a reload.
CARD_VERSION = "0.3.4"
PANEL_PATH = "vega-config"
CARD_FILENAME = "vega-hold-button.js"
CARD_URL = f"/local/vega/{CARD_FILENAME}"

# Config entry keys.
CONF_PORT = "port"
CONF_PIN = "pin"
CONF_PIN2 = "pin2"
CONF_IMEI = "imei"
CONF_COMMANDS_ENABLED = "commands_enabled"  # legacy; superseded by direct_control
CONF_POLL_INTERVAL = "poll_interval"

# Onboarding / mode (two switches): proxy_mode = MITM relay + learn PIN/IMEI;
# direct_control = terminate locally and allow commands.
CONF_PROXY_MODE = "proxy_mode"
CONF_DIRECT_CONTROL = "direct_control"
CONF_UPSTREAM_HOST = "upstream_host"
CONF_UPSTREAM_PORT = "upstream_port"
CONF_LEARNED_PIN = "learned_pin"
CONF_LEARNED_IMEI = "learned_imei"
DEFAULT_UPSTREAM_HOST = "telemetry.evassist.ru"
DEFAULT_UPSTREAM_PORT = 21124
# A fresh integration starts in learn/MITM mode (proxy on, direct off) so it
# begins collecting PIN/IMEI as soon as a tracker is pointed at it.
DEFAULT_PROXY_MODE = True
DEFAULT_DIRECT_CONTROL = False

# Defaults. The tracker's Setting 3001 server slot is re-pointed to this host:port
# by the owner. A high port avoids clashing with whatever already binds :443 on
# the host (HA runs network_mode: host).
DEFAULT_PORT = 21443
DEFAULT_PIN = "0000"
DEFAULT_PIN2 = "0000"
DEFAULT_IMEI = "000000000000000"
DEFAULT_POLL_INTERVAL = 30

# Optional downlink (HA -> tracker) packet logging to the HA event bus.
# Off by default. Each frame HA sends to the tracker — both HA's own
# (inject/direct) and cloud frames relayed through the proxy — is fired as a
# `vega_downlink` event, which the Recorder persists in homeassistant_db and
# shows in Logbook / Developer Tools -> Events. Retention follows the recorder's
# global purge_keep_days.
CONF_LOG_DOWNLINK = "log_downlink"
DEFAULT_LOG_DOWNLINK = False
DOWNLINK_EVENT = "vega_downlink"

PLATFORMS = ["sensor", "binary_sensor", "button", "device_tracker", "switch"]

# Command safety (mirrors the evolute button UX).
BLOCK_OFFLINE = "Машина оффлайн"
BLOCK_IGNITION = "Зажигание включено"
# Commands refused while the ignition is on (don't start/climate a running car).
IGNITION_BLOCKED_COMMANDS = {"remote_on", "preheat", "cooling"}
COMMAND_PENDING_TIMEOUT_SECONDS = 45
COMMAND_REFRESH_DELAYS = (3, 7, 14, 25, 40)

# A session is considered live if a frame was seen within this many seconds.
ONLINE_TIMEOUT = 180

# Map decoded Vega sensor name (SENSOR_DEFS[*]["name"]) -> snapshot state_key.
# The snapshot keys deliberately match the evolute integration's keys where a
# counterpart exists, so dashboards read the same, but entities live under the
# separate ``vega`` device (see entity.py).
SENSOR_KEY_MAP = {
    "soc": "battery_percentage",
    "range_ev": "remains_mileage",
    "battery_12v": "battery_voltage",
    "odometer": "odometer",
    "outside_temp": "outside_temp",
    "inside_temp": "inboard_temp",
    "gsm_csq": "signal_level",
    "speed": "speed",
    "tracker_temp": "tracker_temp",
    "backup_battery": "backup_battery",
    "ignition": "ignition",
    "central_lock": "central_lock",
    "trunk": "trunk",
    "charger_plug": "charging_gun",
    "charging_active": "charging",
    # extra fields (mapped from Combine, matched to evolute cloud)
    "fuel_percentage": "fuel_percentage",
    "remains_mileage_fuel": "remains_mileage_fuel",
    "battery_temp": "battery_temp",
    "coolant_temp": "coolant_temp",
    "climate_target_temp": "climate_target_temp",
    "door_fl": "door_fl",
    "door_fr": "door_fr",
    "door_rl": "door_rl",
    "door_rr": "door_rr",
}

# Sensor definitions: (name, unit, device_class, icon, state_key, state_class)
SENSOR_TYPES = {
    "battery_percentage": ("Battery", "%", SensorDeviceClass.BATTERY, "mdi:battery", "battery_percentage", SensorStateClass.MEASUREMENT),
    "remains_mileage": ("Remaining Range", "km", SensorDeviceClass.DISTANCE, "mdi:map-marker-distance", "remains_mileage", SensorStateClass.MEASUREMENT),
    "battery_voltage": ("12V Battery Voltage", "V", SensorDeviceClass.VOLTAGE, "mdi:car-battery", "battery_voltage", SensorStateClass.MEASUREMENT),
    "backup_battery": ("Backup Battery", "V", SensorDeviceClass.VOLTAGE, "mdi:battery-heart-variant", "backup_battery", SensorStateClass.MEASUREMENT),
    "tracker_temp": ("Tracker Temperature", "°C", SensorDeviceClass.TEMPERATURE, "mdi:thermometer", "tracker_temp", SensorStateClass.MEASUREMENT),
    "odometer": ("Odometer", "km", SensorDeviceClass.DISTANCE, "mdi:counter", "odometer", SensorStateClass.TOTAL_INCREASING),
    "outside_temp": ("Outside Temperature", "°C", SensorDeviceClass.TEMPERATURE, "mdi:thermometer", "outside_temp", SensorStateClass.MEASUREMENT),
    "inboard_temp": ("Cabin Temperature", "°C", SensorDeviceClass.TEMPERATURE, "mdi:thermometer", "inboard_temp", SensorStateClass.MEASUREMENT),
    "battery_temp": ("Battery Temperature", "°C", SensorDeviceClass.TEMPERATURE, "mdi:thermometer", "battery_temp", SensorStateClass.MEASUREMENT),
    "coolant_temp": ("Coolant Temperature", "°C", SensorDeviceClass.TEMPERATURE, "mdi:thermometer", "coolant_temp", SensorStateClass.MEASUREMENT),
    "climate_target_temp": ("Climate Target Temperature", "°C", SensorDeviceClass.TEMPERATURE, "mdi:thermostat", "climate_target_temp", SensorStateClass.MEASUREMENT),
    "fuel_percentage": ("Fuel Level", "%", None, "mdi:gas-station", "fuel_percentage", SensorStateClass.MEASUREMENT),
    "remains_mileage_fuel": ("Remaining Range (Fuel)", "km", SensorDeviceClass.DISTANCE, "mdi:gas-station", "remains_mileage_fuel", SensorStateClass.MEASUREMENT),
    "signal_level": ("GSM Signal", "CSQ", None, "mdi:signal-cellular-3", "signal_level", SensorStateClass.MEASUREMENT),
    "speed": ("Speed", "km/h", SensorDeviceClass.SPEED, "mdi:speedometer", "speed", SensorStateClass.MEASUREMENT),
    "latitude": ("Latitude", "°", None, "mdi:map-marker", "latitude", SensorStateClass.MEASUREMENT),
    "longitude": ("Longitude", "°", None, "mdi:map-marker", "longitude", SensorStateClass.MEASUREMENT),
    "altitude": ("Altitude", "m", SensorDeviceClass.DISTANCE, "mdi:altimeter", "altitude", SensorStateClass.MEASUREMENT),
    "satellites": ("Satellites", None, None, "mdi:satellite-variant", "satellites", SensorStateClass.MEASUREMENT),
    "hdop": ("GPS HDOP", None, None, "mdi:signal", "hdop", SensorStateClass.MEASUREMENT),
    "course": ("Course", "°", None, "mdi:compass", "course", SensorStateClass.MEASUREMENT),
    "position_time": ("Position Time", None, SensorDeviceClass.TIMESTAMP, "mdi:crosshairs-gps", "position_time", None),
    "last_online": ("Last Online", None, SensorDeviceClass.TIMESTAMP, "mdi:clock-outline", "last_online", None),
    # Onboarding diagnostics (text, no state_class).
    "imei": ("Tracker IMEI", None, None, "mdi:identifier", "learned_imei", None),
    "mode": ("Vega Mode", None, None, "mdi:cog-transfer", "mode", None),
}

# Binary sensor definitions: (name, device_class, state_key)
BINARY_SENSOR_TYPES = {
    "ignition": ("Ignition", BinarySensorDeviceClass.RUNNING, "ignition"),
    "central_lock": ("Central Lock", BinarySensorDeviceClass.LOCK, "central_lock"),
    "trunk": ("Trunk", BinarySensorDeviceClass.DOOR, "trunk"),
    "door_fl": ("Driver Door", BinarySensorDeviceClass.DOOR, "door_fl"),
    "door_fr": ("Passenger Door", BinarySensorDeviceClass.DOOR, "door_fr"),
    "door_rl": ("Rear Left Door", BinarySensorDeviceClass.DOOR, "door_rl"),
    "door_rr": ("Rear Right Door", BinarySensorDeviceClass.DOOR, "door_rr"),
    "charging_gun": ("Charging Gun", BinarySensorDeviceClass.PLUG, "charging_gun"),
    "charging": ("Charging", BinarySensorDeviceClass.BATTERY_CHARGING, "charging"),
    "online": ("Online", BinarySensorDeviceClass.CONNECTIVITY, "is_online"),
    "moving": ("Moving", BinarySensorDeviceClass.MOVING, "is_moving"),
    "pin_learned": ("PIN Learned", None, "pin_learned"),
}

# Button definitions: (name, action, icon, confirm_key).
# ``action`` indexes protocol.COMMAND_MAP; ``confirm_key`` names the snapshot key
# whose change confirms the command landed (None = no observable state).
# NOTE: the central-lock device_class on the binary sensor is LOCK, where HA
# treats on=unlocked; the confirm_key simply watches for any change.
# Slot routes are read from Setting 4001; never infer them from these names.
BUTTON_TYPES = {
    "lock": ("Lock", "lock", "mdi:lock", "central_lock"),
    "unlock": ("Unlock", "unlock", "mdi:lock-open", "central_lock"),
    "cooling": ("Cooling On", "cooling", "mdi:snowflake", None),
    "preheat": ("Preheat On", "preheat", "mdi:radiator", None),
    "remote_on": ("Remote On", "remote_on", "mdi:car-connected", None),
    "remote_off": ("Remote Off", "remote_off", "mdi:car-off", None),
    "horn": ("Horn", "horn", "mdi:bullhorn", None),
    "preheat_off": ("Preheat Off", "preheat_off", "mdi:radiator-off", None),
    "cooling_off": ("Cooling Off", "cooling_off", "mdi:snowflake-off", None),
}
