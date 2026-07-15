"""Constants for the Zemismart Blinds integration."""

from typing import Final

DOMAIN: Final = "zemismart_blinds"

CONF_BRIDGE: Final = "bridge"
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
DEFAULT_SNIFF_WINDOW_SECONDS: Final = 30
MAX_SNIFF_WINDOW_SECONDS: Final = 60
FULL_TRAVEL_MARGIN_SECONDS: Final = 1.0
POSITION_UPDATE_INTERVAL_SECONDS: Final = 0.25

# Fixed topic contract shared with the ESPHome RF433 MQTT bridge firmware.
# Existing availability/info/status/tx topics remain unchanged. During the
# guided Learn flow, the bridge publishes non-retained QoS-1 captures to
# rf433/<bridge>/rx as JSON {"frame":"AAB1…55","t":<uint millis>}. The
# controller starts a bounded capture by publishing exactly
# {"action":"sniff","seconds":<0..60>} to rf433/<bridge>/cmd at QoS 1,
# non-retained; seconds=0 cancels the active sniff. Both sides must keep this
# onboarding-only contract identical.
MQTT_ROOT: Final = "rf433"
MQTT_AVAILABILITY_TOPIC: Final = f"{MQTT_ROOT}/+/availability"
MQTT_INFO_TOPIC: Final = f"{MQTT_ROOT}/+/info"
MQTT_STATUS_TOPIC: Final = f"{MQTT_ROOT}/+/status"
MQTT_RX_TOPIC: Final = f"{MQTT_ROOT}/+/rx"
MQTT_CMD_TEMPLATE: Final = f"{MQTT_ROOT}/{{bridge}}/cmd"

MQTT_CMD_ACTION_SNIFF: Final = "sniff"
MQTT_CMD_FIELD_ACTION: Final = "action"
MQTT_CMD_FIELD_SECONDS: Final = "seconds"
MQTT_RX_FIELD_FRAME: Final = "frame"

SERVICE_SEND_RAW: Final = "send_raw"
SERVICE_NEW_VIRTUAL_REMOTE: Final = "new_virtual_remote"

ATTR_BRIDGE: Final = "bridge"
ATTR_RAW: Final = "raw"
ATTR_REPEATS: Final = "repeats"

MANUAL_REMOTE: Final = "manual"
VIRTUAL_REMOTE: Final = "virtual"
