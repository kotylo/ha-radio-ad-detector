"""Configuration for Radio Ad Detector."""
DOMAIN = "radio_ad_detector"
DEFAULT_PORT = 8098
CONF_ADVERTISED_HOST = "advertised_host"
CONF_OUTPUT_FORMAT = "output_format"
CONF_PLAYBACK_BUFFER = "playback_buffer_seconds"
CONF_PORT = "port"
DEFAULT_OUTPUT_FORMAT = "wav"
DEFAULT_PLAYBACK_BUFFER = 2.0
MAX_PLAYBACK_BUFFER = 30
PLATFORMS = ["binary_sensor", "sensor"]
