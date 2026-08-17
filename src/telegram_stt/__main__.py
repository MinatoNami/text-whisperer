from __future__ import annotations

import logging
import sys
from pathlib import Path

from .bot import Bot
from .config import Config, ConfigError, load_dotenv


def main() -> int:
    app_dir = Path(__file__).resolve().parents[2]
    load_dotenv(app_dir / ".env")

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    Bot(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
