"""Command line: `housereflex --config reflexes.json [--once] [--dry-run | --live]`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from . import __version__
from .client import HousevitalsClient
from .config import ConfigError, load_config
from .runner import Runner


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="housereflex", description=__doc__)
    p.add_argument("--config", default=os.environ.get("HOUSEREFLEX_CONFIG"),
                   help="JSON config (see reflexes.example.json); or HOUSEREFLEX_CONFIG")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="only log what would be done")
    mode.add_argument("--live", action="store_true", help="write overrides (overrides dry_run)")
    p.add_argument("--once", action="store_true", help="evaluate once, print the result as JSON")
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not args.config:
        p.error("pass --config or set HOUSEREFLEX_CONFIG")
    try:
        config = load_config(args.config)
        dry_run = True if args.dry_run else False if args.live else config.dry_run
        token = config.token()
    except ConfigError as err:
        sys.exit(f"housereflex: {err}")
    if not dry_run and token is None:
        sys.exit("housereflex: live mode needs a token (HOUSEREFLEX_TOKEN or housevitals.token_file)")
    asyncio.run(_run(config, token, dry_run, args.once))


async def _run(config, token, dry_run, once) -> None:
    client = HousevitalsClient(config.url, token)
    try:
        runner = Runner(config, client, dry_run=dry_run)
        if once:
            print(json.dumps(await runner.tick(), indent=2, ensure_ascii=False))
        else:
            await runner.run()
    finally:
        await client.close()
