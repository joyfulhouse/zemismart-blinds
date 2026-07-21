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
CONF_AIR_ARBITRATION_MODE: Final = "air_arbitration_mode"

DEFAULT_TRAVEL_UP: Final = 15.0
DEFAULT_TRAVEL_DOWN: Final = 15.0
# Three scheduler-level dispatches x the embedded Portisch repeat of 8 puts 24
# OEM-grade frame repetitions on air across three time-diverse ~609 ms windows.
# Two proved insufficient against cross-bridge collisions (missed blinds during
# the 2026-07-20 evening scenes): repeats within one train fall inside the same
# contention window, so only additional time-diverse windows buy reliability.
# Higher values date from before firmware v1.2.0 paced dispatch by airtime
# (most extra repeats then corrupted in the EFM8BB1's UART ring); now every
# repeat transmits, so they only occupy air and delay queued fail-safe STOPs.
DEFAULT_REPEATS: Final = 3
DEFAULT_COALESCE_WINDOW_MS: Final = 150
DEFAULT_SNIFF_WINDOW_SECONDS: Final = 30
MAX_SNIFF_WINDOW_SECONDS: Final = 60
FULL_TRAVEL_MARGIN_SECONDS: Final = 1.0
POSITION_UPDATE_INTERVAL_SECONDS: Final = 0.25

# Fixed topic contract shared with the ESPHome RF433 MQTT bridge firmware.
# Existing availability/info/status/tx topics remain unchanged. The domain
# continuously consumes opt-in, non-retained QoS-1 captures from
# rf433/<bridge>/rx as JSON {"frame":"AAB1…55","t":<uint millis>,"boot":<uint>}.
# During guided Learn, the controller starts a bounded capture by publishing
# exactly
# {"action":"sniff","seconds":<0..60>} to rf433/<bridge>/cmd at QoS 1,
# non-retained; seconds=0 cancels the active sniff. Both sides must keep this
# onboarding command contract identical.
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
