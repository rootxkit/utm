"""The API's reader of live drone state in Redis. P2-05, S-16.

Read the way `gateway.live_state.read_live_state` reads one drone: a key
that has expired is a lost link, and a key without a `state` field is no
state. `get_many` reads a whole fleet in one round trip, so listing drones
does not cost one Redis request per drone.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import redis.asyncio

from common import get_logger
from gateway.live_state import read_live_state, state_key

_log = get_logger(__name__)


@dataclass
class RedisLiveState:
    client: redis.asyncio.Redis

    async def get(self, drone_id: UUID) -> dict[str, Any] | None:
        return await read_live_state(self.client, drone_id)

    async def get_many(
        self, drone_ids: Sequence[UUID]
    ) -> dict[UUID, dict[str, Any] | None]:
        """Each drone's live state, or None where its link is lost.

        One pipelined round trip of `HGET <key> state`. Not MGET: the state
        is a field of a hash, which MGET cannot read.
        """
        if not drone_ids:
            return {}
        async with self.client.pipeline(transaction=False) as pipe:
            for drone_id in drone_ids:
                pipe.hget(state_key(drone_id), "state")
            held: list[bytes | None] = await pipe.execute()
        found: dict[UUID, dict[str, Any] | None] = {}
        for drone_id, state in zip(drone_ids, held, strict=True):
            found[drone_id] = _decode(drone_id, state)
        return found


def _decode(drone_id: UUID, state: bytes | None) -> dict[str, Any] | None:
    """One drone's state. A value that is not a JSON object makes that one
    drone unknown - reported OFFLINE, as an unreadable state is elsewhere -
    rather than failing the whole fleet's list."""
    if state is None:
        return None
    try:
        decoded = json.loads(state)
    except ValueError as error:
        _log.warning(
            "unreadable live state",
            extra={"drone_id": str(drone_id), "error": str(error)},
        )
        return None
    if not isinstance(decoded, dict):
        _log.warning(
            "live state is not an object",
            extra={"drone_id": str(drone_id), "type": type(decoded).__name__},
        )
        return None
    return decoded
