"""Command-line entry point: `claude-xmpp --config config.toml`.

One asyncio loop runs the XMPP bot and the Claude session together.
SIGINT / SIGTERM shut both down; a fatal bot error (bad password, banned)
exits with status 1.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Callable
from typing import Any

from .config import Config, ConfigError, load_config

log = logging.getLogger("claude_xmpp_agent")

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_CONFIG = 2
FAILED_POLL = 1.0  # seconds between checks of bot.failed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="claude-xmpp",
        description="Control a Claude Code session from an XMPP group chat.",
    )
    p.add_argument(
        "-c", "--config", default="config.toml",
        help="path to the TOML config file (default: %(default)s)",
    )
    p.add_argument("--resume", metavar="SESSION_ID", help="resume this Claude session on start")
    p.add_argument("--debug", action="store_true", help="debug logging (very verbose)")
    return p.parse_args(argv)


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not debug:
        # slixmpp logs every stream event at INFO
        logging.getLogger("slixmpp").setLevel(logging.WARNING)


def _default_parts(config: Config) -> tuple[Any, Any]:
    from .bot import Bot
    from .commands import Router
    from .session import Session

    bot = Bot(config)
    session = Session(config, output=bot.output, on_busy=bot.set_busy)
    bot.router = Router(session, config)
    return bot, session


async def run(
    config: Config,
    *,
    resume: str | None = None,
    stop: asyncio.Event | None = None,
    make_parts: Callable[[Config], tuple[Any, Any]] = _default_parts,
) -> int:
    """Run until `stop` is set (by a signal) or the bot fails. Returns an exit code."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)

    bot, session = make_parts(config)
    code = EXIT_OK
    try:
        log.info("starting Claude session in %s", config.claude.cwd)
        try:
            await session.start(resume=resume)
        except Exception as e:  # noqa: BLE001
            log.error("cannot start Claude: %s", e)
            return EXIT_FATAL
        log.info("connecting to XMPP as %s", config.xmpp.jid)
        await bot.start()
        while not stop.is_set() and not bot.failed:
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), FAILED_POLL)
        if bot.failed:
            log.error("fatal: %s", bot.failed)
            code = EXIT_FATAL
        else:
            log.info("shutting down")
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)
        with contextlib.suppress(Exception):
            await session.close()
        with contextlib.suppress(Exception):
            await bot.stop()
    return code


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    setup_logging(args.debug)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"claude-xmpp: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)
    try:
        code = asyncio.run(run(config, resume=args.resume))
    except KeyboardInterrupt:  # platforms without loop signal handlers
        code = EXIT_OK
    sys.exit(code)


if __name__ == "__main__":
    main()
