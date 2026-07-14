"""Constants for the Zemismart Blinds integration."""

from typing import Final

DOMAIN: Final = "zemismart_blinds"

CONF_NAME: Final = "name"
CONF_PREFIX: Final = "prefix"
CONF_REMOTE_ID: Final = "remote_id"
CONF_KNOWN_REMOTE: Final = "known_remote"
CONF_CALIBRATION_BUTTON: Final = "calibration_button"
CONF_CALIBRATION_BASE: Final = "calibration_base"
CONF_CALIBRATION_FRAME: Final = "calibration_frame"
CONF_BASE_UP: Final = "base_up"
CONF_BASE_DOWN: Final = "base_down"
CONF_BASE_STOP: Final = "base_stop"
CONF_BASE_TRAILER: Final = "base_trailer"
CONF_CHANNELS: Final = "channels"
CONF_TRAVEL_UP: Final = "travel_up"
CONF_TRAVEL_DOWN: Final = "travel_down"
CONF_AREA_ID: Final = "area_id"
CONF_REPEATS: Final = "repeats"
CONF_COALESCE_WINDOW_MS: Final = "coalesce_window_ms"

DEFAULT_TRAVEL_UP: Final = 15.0
DEFAULT_TRAVEL_DOWN: Final = 15.0
DEFAULT_REPEATS: Final = 5
DEFAULT_COALESCE_WINDOW_MS: Final = 150
FULL_TRAVEL_MARGIN_SECONDS: Final = 1.0
POSITION_UPDATE_INTERVAL_SECONDS: Final = 0.25

# Fixed topic contract shared with the ESPHome RF433 MQTT bridge firmware.
# The bridge publishes rf433/<bridge>/{availability,info,status} and consumes
# rf433/<bridge>/tx; both sides must agree, so this is a constant by design.
MQTT_ROOT: Final = "rf433"
MQTT_AVAILABILITY_TOPIC: Final = f"{MQTT_ROOT}/+/availability"
MQTT_INFO_TOPIC: Final = f"{MQTT_ROOT}/+/info"
MQTT_STATUS_TOPIC: Final = f"{MQTT_ROOT}/+/status"

SERVICE_SEND_RAW: Final = "send_raw"
SERVICE_NEW_VIRTUAL_REMOTE: Final = "new_virtual_remote"

ATTR_BRIDGE: Final = "bridge"
ATTR_RAW: Final = "raw"
ATTR_REPEATS: Final = "repeats"

MANUAL_REMOTE: Final = "manual"
VIRTUAL_REMOTE: Final = "virtual"
