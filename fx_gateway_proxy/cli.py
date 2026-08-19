import os
import argparse
import logging
import uvicorn

from .config import DEFAULT_HOST, DEFAULT_PORT, __version__
from .server import app

logger = logging.getLogger("fx-gateway-proxy")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fx-gateway-proxy",
        description="FX Gateway Proxy: OpenAI-compatible reverse proxy for Vercel AI Gateway FX promotional free pool with Adaptive Multi-Key Routing",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")
    parser.add_argument("--api-key", default=None, help="Vercel AI Gateway API key (overrides AI_GATEWAY_API_KEY / ~/.fx/api-key)")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"], help="Logging level (default: info)")
    parser.add_argument("--version", action="version", version=f"fx-gateway-proxy {__version__}")

    args = parser.parse_args()

    if args.api_key:
        os.environ["AI_GATEWAY_API_KEY"] = args.api_key.strip()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger.info(f"Starting FX Gateway Proxy v{__version__} on http://{args.host}:{args.port} ...")

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
