"""Support for Ariston sensors."""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from ariston.const import ConsumptionTimeInterval
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import slugify

from .const import ARISTON_SENSOR_TYPES, DOMAIN, AristonSensorEntityDescription
from .coordinator import DeviceDataUpdateCoordinator
from .energy_slots import async_update_slot_statistics
from .entity import AristonEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the Ariston sensors from config entry."""
    ariston_sensors: list[AristonSensor] = []

    for description in ARISTON_SENSOR_TYPES:
        coordinator: DeviceDataUpdateCoordinator = hass.data[DOMAIN][entry.unique_id][
            description.coordinator
        ]
        if (
            coordinator
            and coordinator.device
            and coordinator.device.are_device_features_available(
                description.device_features,
                description.system_types,
                description.whe_types,
            )
        ):
            ariston_sensors.append(
                AristonSensor(
                    coordinator,
                    description,
                )
            )

    async_add_entities(ariston_sensors)


class AristonSensor(AristonEntity, SensorEntity):
    """Base class for specific ariston sensors."""

    def __init__(
        self,
        coordinator: DeviceDataUpdateCoordinator,
        description: AristonSensorEntityDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, description)

    @property
    def native_value(self):
        """Return value of sensor."""
        return self.entity_description.get_native_value(self)

    @property
    def native_unit_of_measurement(self):
        """Return the nateive unit of measurement."""
        if self.entity_description.get_native_unit_of_measurement is not None:
            return self.entity_description.get_native_unit_of_measurement(self)

        if self.entity_description.native_unit_of_measurement is not None:
            return self.entity_description.native_unit_of_measurement

        return None

    @property
    def last_reset(self) -> datetime | None:
        """Return the time when the sensor was last reset, if any."""
        if self.entity_description.get_last_reset is not None:
            return self.entity_description.get_last_reset(self)

        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle new data and, for energy sensors, publish the 2 h slots."""
        super()._handle_coordinator_update()
        description = self.entity_description
        if (
            description.slots_consumption_type is not None
            and description.slots_statistic_suffix is not None
        ):
            self.hass.async_create_task(
                self._async_update_slot_statistics(),
                f"ariston slot statistics {self.entity_id}",
            )

    async def _async_update_slot_statistics(self) -> None:
        """Write the real per-slot energy values to long-term statistics."""
        description = self.entity_description
        try:
            vals = None
            for sequence in self.device.consumptions_sequences:
                if (
                    sequence["k"] == description.slots_consumption_type.value
                    and sequence["p"] == ConsumptionTimeInterval.LAST_DAY.value
                ):
                    vals = [float(value) for value in sequence["v"]]
                    break
            if not vals:
                return
            device_slug = slugify(self.device.name or self.device.serial_number or "")
            await async_update_slot_statistics(
                self.hass,
                self.entity_id,
                f"{DOMAIN}:{device_slug}_{description.slots_statistic_suffix}",
                f"{self.device.name} {description.slots_statistic_suffix.replace('_', ' ')} (2 h slots)",
                vals,
            )
        except Exception:  # noqa: BLE001 - never break the sensor over statistics
            _LOGGER.exception("Could not update slot statistics for %s", self.entity_id)
