"""
Support for Taps Aff binary sensor.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/binary_sensor.tapsaff/
"""
from datetime import timedelta
import logging

import voluptuous as vol

from homeassistant.components.binary_sensor \
    import (BinarySensorDevice, PLATFORM_SCHEMA)
from homeassistant.const import (CONF_NAME)
import homeassistant.helpers.config_validation as cv
from homeassistant.util import Throttle

REQUIREMENTS = ['tapsaff==0.1.0']

CONF_LOCATION = "location"

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = 'Taps Aff'

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=30)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Required(CONF_LOCATION): cv.string,
})


def setup_platform(hass, config, add_devices, discovery_info=None):
    """Set up the taps aff sensor."""
    name = config.get(CONF_NAME)
    location = config.get(CONF_LOCATION)

    taps_aff_data = TapsAffData(location)

    add_devices([TapsAffSensor(taps_aff_data, name)], True)


class TapsAffSensor(BinarySensorDevice):
    """Implementation of a taps aff sensor."""

    def __init__(self, taps_aff_data, name):
        """Initialize the sensor."""
        self.data = taps_aff_data
        self._name = name

    @property
    def name(self):
        """Return the name of the sensor."""
        return '{}'.format(self._name)

    @property
    def is_on(self):
        """Return true if taps aff."""
        return self.data.is_taps_aff

    def update(self):
        """Get the latest data."""
        self.data.update()


class TapsAffData(object):
    """Class for handling the data retrieval for pins."""

    def __init__(self, location):
        """Initialize the sensor."""
        from tapsaff import TapsAff

        self._is_taps_aff = None
        self.taps_aff = TapsAff(location)

    @property
    def is_taps_aff(self):
        """Return true if taps aff."""
        return self._is_taps_aff

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def update(self):
        """Get the latest data from the Taps Aff API and updates the states."""
        self._is_taps_aff = self.taps_aff.is_taps_aff
