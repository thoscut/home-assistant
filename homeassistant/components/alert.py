"""
Support for repeating alerts when conditions are met.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/alert/
"""
import asyncio
from datetime import datetime, timedelta
import logging

import voluptuous as vol

from homeassistant.components.notify import (
    ATTR_MESSAGE, DOMAIN as DOMAIN_NOTIFY)
from homeassistant.const import (
    CONF_ENTITY_ID, STATE_IDLE, CONF_NAME, CONF_STATE, STATE_ON, STATE_OFF,
    SERVICE_TURN_ON, SERVICE_TURN_OFF, SERVICE_TOGGLE, ATTR_ENTITY_ID)
from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers import service, event
import homeassistant.helpers.config_validation as cv

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'alert'
ENTITY_ID_FORMAT = DOMAIN + '.{}'

CONF_CAN_ACK = 'can_acknowledge'
CONF_NOTIFIERS = 'notifiers'
CONF_REPEAT = 'repeat'
CONF_SKIP_FIRST = 'skip_first'
CONF_MESSAGE = 'message'
CONF_DONE_MESSAGE = 'done_message'

DEFAULT_CAN_ACK = True
DEFAULT_SKIP_FIRST = False

ALERT_SCHEMA = vol.Schema({
    vol.Required(CONF_NAME): cv.string,
    vol.Required(CONF_ENTITY_ID): cv.entity_id,
    vol.Required(CONF_STATE, default=STATE_ON): cv.string,
    vol.Required(CONF_REPEAT): vol.All(cv.ensure_list, [vol.Coerce(float)]),
    vol.Required(CONF_CAN_ACK, default=DEFAULT_CAN_ACK): cv.boolean,
    vol.Required(CONF_SKIP_FIRST, default=DEFAULT_SKIP_FIRST): cv.boolean,
    vol.Optional(CONF_MESSAGE): cv.template,
    vol.Optional(CONF_DONE_MESSAGE): cv.string,
    vol.Required(CONF_NOTIFIERS): cv.ensure_list})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        cv.slug: ALERT_SCHEMA,
    }),
}, extra=vol.ALLOW_EXTRA)


ALERT_SERVICE_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
})


def is_on(hass, entity_id):
    """Return if the alert is firing and not acknowledged."""
    return hass.states.is_state(entity_id, STATE_ON)


async def async_setup(hass, config):
    """Set up the Alert component."""
    entities = []

    for object_id, cfg in config[DOMAIN].items():
        if not cfg:
            cfg = {}

        name = cfg.get(CONF_NAME)
        watched_entity_id = cfg.get(CONF_ENTITY_ID)
        alert_state = cfg.get(CONF_STATE)
        repeat = cfg.get(CONF_REPEAT)
        skip_first = cfg.get(CONF_SKIP_FIRST)
        message = cfg.get(CONF_MESSAGE)
        done_message = cfg.get(CONF_DONE_MESSAGE)
        notifiers = cfg.get(CONF_NOTIFIERS)
        can_ack = cfg.get(CONF_CAN_ACK)

        entities.append(Alert(hass, object_id, name, watched_entity_id,
                              alert_state, repeat, skip_first, message,
                              done_message, notifiers, can_ack))

    if not entities:
        return False

    async def async_handle_alert_service(service_call):
        """Handle calls to alert services."""
        alert_ids = service.extract_entity_ids(hass, service_call)

        for alert_id in alert_ids:
            for alert in entities:
                if alert.entity_id != alert_id:
                    continue

                alert.async_set_context(service_call.context)
                if service_call.service == SERVICE_TURN_ON:
                    await alert.async_turn_on()
                elif service_call.service == SERVICE_TOGGLE:
                    await alert.async_toggle()
                else:
                    await alert.async_turn_off()

    # Setup service calls
    hass.services.async_register(
        DOMAIN, SERVICE_TURN_OFF, async_handle_alert_service,
        schema=ALERT_SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_TURN_ON, async_handle_alert_service,
        schema=ALERT_SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_TOGGLE, async_handle_alert_service,
        schema=ALERT_SERVICE_SCHEMA)

    tasks = [alert.async_update_ha_state() for alert in entities]
    if tasks:
        await asyncio.wait(tasks, loop=hass.loop)

    return True


class Alert(ToggleEntity):
    """Representation of an alert."""

    def __init__(self, hass, entity_id, name, done_message, watched_entity_id,
                 state, repeat, skip_first, message, notifiers,
                 can_ack):
        """Initialize the alert."""
        self.hass = hass
        self._name = name
        self._alert_state = state
        self._skip_first = skip_first

        self._message = message
        if self._message is not None:
            self._message.hass = hass

        self._notifiers = notifiers
        self._can_ack = can_ack
        self._done_message = done_message

        self._delay = [timedelta(minutes=val) for val in repeat]
        self._next_delay = 0

        self._firing = False
        self._ack = False
        self._cancel = None
        self._send_done_message = False
        self.entity_id = ENTITY_ID_FORMAT.format(entity_id)

        event.async_track_state_change(
            hass, watched_entity_id, self.watched_entity_change)

    @property
    def name(self):
        """Return the name of the alert."""
        return self._name

    @property
    def should_poll(self):
        """HASS need not poll these entities."""
        return False

    @property
    def state(self):
        """Return the alert status."""
        if self._firing:
            if self._ack:
                return STATE_OFF
            return STATE_ON
        return STATE_IDLE

    @property
    def hidden(self):
        """Hide the alert when it is not firing."""
        return not self._can_ack or not self._firing

    async def watched_entity_change(self, entity, from_state, to_state):
        """Determine if the alert should start or stop."""
        _LOGGER.debug("Watched entity (%s) has changed", entity)
        if to_state.state == self._alert_state and not self._firing:
            await self.begin_alerting()
        if to_state.state != self._alert_state and self._firing:
            await self.end_alerting()

    async def begin_alerting(self):
        """Begin the alert procedures."""
        _LOGGER.debug("Beginning Alert: %s", self._name)
        self._ack = False
        self._firing = True
        self._next_delay = 0

        if not self._skip_first:
            await self._notify()
        else:
            await self._schedule_notify()

        self.async_schedule_update_ha_state()

    async def end_alerting(self):
        """End the alert procedures."""
        _LOGGER.debug("Ending Alert: %s", self._name)
        self._cancel()
        self._ack = False
        self._firing = False
        if self._send_done_message:
            await self._notify_done_message()
        self.async_schedule_update_ha_state()

    async def _schedule_notify(self):
        """Schedule a notification."""
        delay = self._delay[self._next_delay]
        next_msg = datetime.now() + delay
        self._cancel = \
            event.async_track_point_in_time(self.hass, self._notify, next_msg)
        self._next_delay = min(self._next_delay + 1, len(self._delay) - 1)

    async def _notify(self, *args):
        """Send the alert notification."""
        if not self._firing:
            return

        if not self._ack:
            _LOGGER.info("Alerting: %s", self._name)
            self._send_done_message = True

            if self._message is not None:
                message = self._message.async_render()
            else:
                message = self._name

            await _send_message(message)
        await self._schedule_notify()

    async def _notify_done_message(self, *args):
        """Send notification of complete alert."""
        _LOGGER.info("Alerting: %s", self._done_message)
        self._send_done_message = False
		
        if self._done_message is None:
            return

        message = self._done_message.async_render()

        await _send_message(message)

    async def _send_message(self, message):
        for target in self._notifiers:
            await self.hass.services.async_call(
                DOMAIN_NOTIFY, target, {ATTR_MESSAGE: message})

    async def async_turn_on(self, **kwargs):
        """Async Unacknowledge alert."""
        _LOGGER.debug("Reset Alert: %s", self._name)
        self._ack = False
        await self.async_update_ha_state()

    async def async_turn_off(self, **kwargs):
        """Async Acknowledge alert."""
        _LOGGER.debug("Acknowledged Alert: %s", self._name)
        self._ack = True
        await self.async_update_ha_state()

    async def async_toggle(self, **kwargs):
        """Async toggle alert."""
        if self._ack:
            return await self.async_turn_on()
        return await self.async_turn_off()
