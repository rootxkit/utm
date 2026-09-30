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

from gateway.live_state import read_live_state, state_key


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
            decoded: dict[str, Any] | None = (
                None if state is None else json.loads(state)
            )
            found[drone_id] = decoded
        return found
