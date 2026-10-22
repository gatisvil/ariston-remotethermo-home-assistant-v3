"""Real per-slot Ariston energy in Home Assistant long-term statistics.

The Ariston cloud returns, for every consumption type, a list of 24 values for
the "last day" (``p == 1``). Each value is the energy of one 2 hour slot and
the last value is the newest slot. The integration sensors only expose the
last value (``v[-1]``), and Home Assistant books it into the hour in which the
update arrived. Next to a whole-home meter this shows up as "untracked" and
"over-reported" bars in the Energy dashboard.

This module writes every slot of that list to its own external statistic
(``ariston:<device>_<type>_energy``), each slot split evenly over its two
hours and placed at the hours it was really used. Use these statistics as the
devices in the Energy dashboard.

Slot timing (calibrated against a whole-home meter over three weeks): a slot
ends on an even local clock hour and is published to the cloud shortly after,
i.e. the value seen at 12:45 is the energy of 10:00-12:00.

Every refresh rewrites the whole 48 h window from the cloud list, so late or
corrected values heal themselves. On the very first run the older history is
rebuilt from the long term statistics of the existing sensor (blocks spread
over the two hours before the even clock hour of the update).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SLOT = timedelta(hours=2)
HOUR = timedelta(hours=1)
UPDATE_DELAY = timedelta(minutes=45)  # old sensor: update arrives ~45 min after slot end
STORE_VERSION = 1


# --------------------------------------------------------------------------
# pure helpers (no Home Assistant objects, unit-testable)
# --------------------------------------------------------------------------
def floor_even_hour(moment: datetime, tz: tzinfo) -> datetime:
    """Latest even local clock hour at or before ``moment`` (returned in UTC)."""
    local = moment.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    utc = local.astimezone(timezone.utc)
    if local.hour % 2:
        utc -= HOUR
    return utc


def matching_shifts(old: list[float], new: list[float]) -> list[int]:
    """Return every k (1..n-1) for which the window moved k slots.

    ``new[:-k] == old[k:]`` means the newest k slots are new, the rest is the
    old data moved to the left.
    """
    if len(old) != len(new):
        return []
    n = len(new)
    return [k for k in range(1, n) if new[: n - k] == old[k:]]


def choose_shift(
    candidates: list[int], last_end: datetime | None, now: datetime, tz: tzinfo
) -> int | None:
    """Pick the shift: the matching one closest to the wall clock expectation."""
    expected: int | None = None
    if last_end is not None:
        expected = max(1, round((floor_even_hour(now, tz) - last_end) / SLOT))
    if candidates:
        if expected is None:
            return candidates[0]
        return min(candidates, key=lambda k: (abs(k - expected), k))
    return expected  # no clean match (values were corrected): trust the clock


def live_rows(vals: list[float], end: datetime, base: float) -> list[StatisticData]:
    """Hourly cumulative rows for slot values whose newest slot ends at ``end``."""
    start = end - SLOT * len(vals)
    total = base
    rows: list[StatisticData] = []
    for i, value in enumerate(vals):
        for j in range(2):
            total += value / 2
            rows.append(
                StatisticData(
                    start=start + SLOT * i + HOUR * j,
                    state=round(total, 6),
                    sum=round(total, 6),
                )
            )
    return rows


def backfill_rows(
    changes: list[tuple[datetime, float]], before: datetime, tz: tzinfo
) -> list[StatisticData]:
    """Rebuild history from the old sensor's hourly ``change`` statistics.

    ``changes`` are (hour start, kWh booked in that hour). Each block is spread
    over the two hours before the even clock hour of its update time. Only
    hours before ``before`` are returned; the sum starts at 0.
    """
    buckets: dict[datetime, float] = {}
    for hour_start, kwh in changes:
        if not kwh or kwh <= 0:
            continue
        end = floor_even_hour(hour_start + UPDATE_DELAY, tz)
        if end > before:
            continue
        for j in (2, 1):
            key = end - HOUR * j
            buckets[key] = buckets.get(key, 0.0) + kwh / 2
    total = 0.0
    rows: list[StatisticData] = []
    for start in sorted(buckets):
        total += buckets[start]
        rows.append(
            StatisticData(start=start, state=round(total, 6), sum=round(total, 6))
        )
    return rows


# --------------------------------------------------------------------------
# Home Assistant glue
# --------------------------------------------------------------------------
def _metadata(statistic_id: str, name: str) -> StatisticMetaData:
    return StatisticMetaData(
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


async def _sum_before(
    hass: HomeAssistant, statistic_id: str, when: datetime
) -> float | None:
    """Cumulative sum of the last stored hour before ``when`` (None if none)."""
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        when - timedelta(days=60),
        when,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    rows = result.get(statistic_id)
    if rows and rows[-1].get("sum") is not None:
        return float(rows[-1]["sum"])
    return None


async def _history_changes(
    hass: HomeAssistant, entity_id: str, before: datetime
) -> list[tuple[datetime, float]]:
    """Hourly 'change' statistics of the old sensor (long-term, all history)."""
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2015, 1, 1, tzinfo=timezone.utc),
        before,
        {entity_id},
        "hour",
        None,
        {"change"},
    )
    return [
        (dt_util.utc_from_timestamp(row["start"]), float(row["change"]))
        for row in result.get(entity_id, [])
        if row.get("change")
    ]


async def async_update_slot_statistics(
    hass: HomeAssistant,
    entity_id: str,
    statistic_id: str,
    name: str,
    vals: list[float],
    now: datetime | None = None,
) -> None:
    """Called after each energy refresh with the slot values of one type."""
    now = now or dt_util.utcnow()
    tz = dt_util.get_default_time_zone()
    store: Store[dict[str, Any]] = Store(
        hass, STORE_VERSION, f"{DOMAIN}.slots.{statistic_id.split(':', 1)[1]}"
    )
    saved = await store.async_load() or {}
    old: list[float] | None = saved.get("vals")
    last_end = dt_util.parse_datetime(saved["end"]) if saved.get("end") else None

    if old is None:  # first sight: wait for the next publication to anchor
        await store.async_save({"vals": vals, "end": None})
        return
    if vals == old:  # nothing new from the cloud
        return

    candidates = matching_shifts(old, vals)
    if last_end is None:
        # Anchor only on a clean one-slot shift; the slot that just appeared
        # ends on the latest even local hour.
        if 1 not in candidates:
            await store.async_save({"vals": vals, "end": None})
            return
        end = floor_even_hour(now, tz)
    else:
        shift = choose_shift(candidates, last_end, now, tz)
        end = last_end + SLOT * (shift or 1)

    window_start = end - SLOT * len(vals)
    base = await _sum_before(hass, statistic_id, window_start)
    rows: list[StatisticData] = []
    if base is None:  # nothing stored yet: rebuild older history first
        history = await _history_changes(hass, entity_id, window_start)
        rows = backfill_rows(history, window_start, tz)
        base = rows[-1]["sum"] if rows else 0.0
        _LOGGER.info(
            "%s: rebuilt %d hourly rows of history from %s",
            statistic_id,
            len(rows),
            entity_id,
        )
    rows += live_rows(vals, end, base)
    async_add_external_statistics(hass, _metadata(statistic_id, name), rows)
    await store.async_save({"vals": vals, "end": end.isoformat()})
    _LOGGER.debug(
        "%s: wrote %d hourly rows, newest slot ends %s",
        statistic_id,
        len(rows),
        end.astimezone(tz),
    )
