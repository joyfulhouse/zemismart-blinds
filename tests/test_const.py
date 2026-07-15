"""Tests for the shared Zemismart MQTT contract constants."""

from custom_components.zemismart_blinds.const import (
    CONF_BRIDGE,
    DEFAULT_SNIFF_WINDOW_SECONDS,
    MAX_SNIFF_WINDOW_SECONDS,
    MQTT_CMD_ACTION_SNIFF,
    MQTT_CMD_FIELD_ACTION,
    MQTT_CMD_FIELD_SECONDS,
    MQTT_CMD_TEMPLATE,
    MQTT_RX_FIELD_FRAME,
    MQTT_RX_TOPIC,
)


def test_mqtt_sniff_command_contract() -> None:
    """Sniff constants encode the firmware's bounded onboarding contract."""
    assert MQTT_CMD_TEMPLATE.format(bridge="bridge-a") == "rf433/bridge-a/cmd"
    assert MQTT_CMD_FIELD_ACTION == "action"
    assert MQTT_CMD_FIELD_SECONDS == "seconds"
    assert MQTT_CMD_ACTION_SNIFF == "sniff"
    assert DEFAULT_SNIFF_WINDOW_SECONDS == 30
    assert MAX_SNIFF_WINDOW_SECONDS == 60
    assert CONF_BRIDGE == "bridge"


def test_mqtt_receive_contract() -> None:
    """Receive constants encode the flow-local capture topic and frame field."""
    assert MQTT_RX_TOPIC == "rf433/+/rx"
    assert MQTT_RX_FIELD_FRAME == "frame"
