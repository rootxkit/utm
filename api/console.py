"""Run the P1-08 console feed.

A module rather than a `uvicorn --factory` command line, because the factory
takes an argument and passing one through a Makefile recipe means quoting it
twice.

Configuration comes from `ConsoleSettings`, never from `os.environ` - CLAUDE.md,
and enforced by `tests/test_no_direct_environ.py`, which caught the first
version of this file doing exactly that.
"""

from __future__ import annotations

import uvicorn

from api.config import ConsoleSettings
from api.telemetry_ws import create_app
from common import configure_logging, load_settings


def main() -> None:
    settings = load_settings(ConsoleSettings)
    configure_logging(service=settings.service_name, level=settings.log_level.value)
    uvicorn.run(
        create_app(
            str(settings.nats_url),
            feed_secret=settings.feed_ticket_secret.get_secret_value().encode("utf-8"),
            basemap_dir=settings.basemap_dir,
            allowed_origins=settings.allowed_origins,
        ),
        host=settings.console_host,
        port=settings.console_port,
        # No sign-in here, but its logs should name the real client too.
        **settings.uvicorn_proxy_options(),
    )


if __name__ == "__main__":
    main()
