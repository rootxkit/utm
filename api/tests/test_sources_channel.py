"""How the API connects its control channel. U-15.

nats-py's default gives up reconnecting after about two minutes, and the
client is then closed for good; the API's channel must not be one of those.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from api.sources import NatsControlChannel, nats_channel_factory
from common.bus import RECONNECT_FOREVER


class NoJetStreamClient:
    is_closed = False

    def jetstream(self) -> Any:
        return self

    async def key_value(self, bucket: str) -> Any:
        raise ConnectionError("JetStream not enabled")


async def test_the_channel_reconnects_for_ever_and_survives_a_missing_bucket(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: dict[str, Any] = {}

    async def connect(url: str, **options: Any) -> NoJetStreamClient:
        seen["url"], seen["options"] = url, options
        return NoJetStreamClient()

    factory = nats_channel_factory(
        "nats://broker:4222", bucket="b", subject="s", connect=connect
    )
    with caplog.at_level(logging.ERROR, logger="api.sources"):
        channel = await factory()

    assert seen == {
        "url": "nats://broker:4222",
        "options": {"max_reconnect_attempts": RECONNECT_FOREVER},
    }
    assert RECONNECT_FOREVER == -1
    # Returned anyway: the bucket is made at the first write instead.
    assert isinstance(channel, NatsControlChannel)
    assert not channel.closed
    assert any(
        "could not make the source control bucket" in r.getMessage()
        for r in caplog.records
    )
