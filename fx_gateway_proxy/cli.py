import os
import sys
import argparse
import uvicorn

from .config import DEFAULT_HOST, DEFAULT_PORT

def main():
    parser = argparse.ArgumentParser(
        description="FX Gateway Proxy: OpenAI-compatible reverse proxy for Vercel AI Gateway FX promotional free pool"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")
    parser.add_argument("--api-key", default=None, help="Vercel AI Gateway API key (overrides AI_GATEWAY_API_KEY / ~/.fx/api-key)")
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"], help="Logging level (default: info)")
    parser.add_argument("--workers", type=int, default=1, help="Number of uvicorn worker processes (default: 1)")

    args = parser.parse_args()

    if args.api_key:
        os.environ["AI_GATEWAY_API_KEY"] = args.api_key.strip()

    print(f"Starting FX Gateway Proxy on http://{args.host}:{args.port} ...")
    uvicorn.run(
        "fx_gateway_proxy.server:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        workers=args.workers,
    )

if __name__ == "__main__":
    main()
