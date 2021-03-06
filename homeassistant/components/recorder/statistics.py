"""Statistics helper."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from itertools import groupby
import logging
from typing import TYPE_CHECKING

from sqlalchemy import bindparam
from sqlalchemy.ext import baked

import homeassistant.util.dt as dt_util

from .const import DOMAIN
from .models import Statistics, process_timestamp_to_utc_isoformat
from .util import execute, retryable_database_job, session_scope

if TYPE_CHECKING:
    from . import Recorder

QUERY_STATISTICS = [
    Statistics.statistic_id,
    Statistics.start,
    Statistics.mean,
    Statistics.min,
    Statistics.max,
]

STATISTICS_BAKERY = "recorder_statistics_bakery"

_LOGGER = logging.getLogger(__name__)


def async_setup(hass):
    """Set up the history hooks."""
    hass.data[STATISTICS_BAKERY] = baked.bakery()


def get_start_time() -> datetime.datetime:
    """Return start time."""
    last_hour = dt_util.utcnow() - timedelta(hours=1)
    start = last_hour.replace(minute=0, second=0, microsecond=0)
    return start


@retryable_database_job("statistics")
def compile_statistics(instance: Recorder, start: datetime.datetime) -> bool:
    """Compile statistics."""
    start = dt_util.as_utc(start)
    end = start + timedelta(hours=1)
    _LOGGER.debug(
        "Compiling statistics for %s-%s",
        start,
        end,
    )
    platform_stats = []
    for domain, platform in instance.hass.data[DOMAIN].items():
        if not hasattr(platform, "compile_statistics"):
            continue
        platform_stats.append(platform.compile_statistics(instance.hass, start, end))
        _LOGGER.debug(
            "Statistics for %s during %s-%s: %s", domain, start, end, platform_stats[-1]
        )

    with session_scope(session=instance.get_session()) as session:  # type: ignore
        for stats in platform_stats:
            for entity_id, stat in stats.items():
                session.add(Statistics.from_stats(DOMAIN, entity_id, start, stat))

    return True


def statistics_during_period(hass, start_time, end_time=None, statistic_id=None):
    """Return states changes during UTC period start_time - end_time."""
    with session_scope(hass=hass) as session:
        baked_query = hass.data[STATISTICS_BAKERY](
            lambda session: session.query(*QUERY_STATISTICS)
        )

        baked_query += lambda q: q.filter(Statistics.start >= bindparam("start_time"))

        if end_time is not None:
            baked_query += lambda q: q.filter(Statistics.start < bindparam("end_time"))

        if statistic_id is not None:
            baked_query += lambda q: q.filter_by(statistic_id=bindparam("statistic_id"))
            statistic_id = statistic_id.lower()

        baked_query += lambda q: q.order_by(Statistics.statistic_id, Statistics.start)

        stats = execute(
            baked_query(session).params(
                start_time=start_time, end_time=end_time, statistic_id=statistic_id
            )
        )

        statistic_ids = [statistic_id] if statistic_id is not None else None

        return _sorted_statistics_to_dict(
            hass, session, stats, start_time, statistic_ids
        )


def _sorted_statistics_to_dict(
    hass,
    session,
    stats,
    start_time,
    statistic_ids,
):
    """Convert SQL results into JSON friendly data structure."""
    result = defaultdict(list)
    # Set all statistic IDs to empty lists in result set to maintain the order
    if statistic_ids is not None:
        for stat_id in statistic_ids:
            result[stat_id] = []

    # Called in a tight loop so cache the function
    # here
    _process_timestamp_to_utc_isoformat = process_timestamp_to_utc_isoformat

    # Append all changes to it
    for ent_id, group in groupby(stats, lambda state: state.statistic_id):
        ent_results = result[ent_id]
        ent_results.extend(
            {
                "statistic_id": db_state.statistic_id,
                "start": _process_timestamp_to_utc_isoformat(db_state.start),
                "mean": db_state.mean,
                "min": db_state.min,
                "max": db_state.max,
            }
            for db_state in group
        )

    # Filter out the empty lists if some states had 0 results.
    return {key: val for key, val in result.items() if val}
