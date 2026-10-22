"""SRT + SCTE-35 + VAST server-side ad insertion proof of concept."""

import logging


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)-9s %(message)s",
        datefmt="%H:%M:%S",
    )
