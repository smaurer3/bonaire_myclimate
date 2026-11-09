"""Constants for the Bonaire MyClimate integration."""

DEVICE_MANUFACTURER = "Bonaire"
DEVICE_MODEL = "MyClimate Wi-Fi"
DEVICE_NAME = "Bonaire MyClimate"

DOMAIN = "bonaire_myclimate"

MAX_TEMP = 32
MIN_TEMP = 10
PRECISION = "1.0"
TEMPERATURE_STEP = 1


# Config entry data keys (CONF_HOST is homeassistant.const.CONF_HOST, "host")
# "host"      - optional IP address entered by the user; blank/absent means "find it automatically"
# "last_host" - the IP address the module last connected back from (kept up to date automatically)
# "serial"    - the module's serial number, used as the config entry's unique id
CONF_LAST_HOST = "last_host"
CONF_SERIAL = "serial"
