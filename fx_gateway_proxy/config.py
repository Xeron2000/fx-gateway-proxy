import os
from pathlib import Path

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "https://ai-gateway.vercel.sh/v3/ai/language-model")
USER_AGENT = os.environ.get("FX_USER_AGENT", "fx/0.0.3")
DEFAULT_KEY_PATH = Path.home() / ".fx" / "api-key"
DEFAULT_HOST = os.environ.get("HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PORT", "18080"))

def get_api_key(explicit_key: str = "") -> str:
    """Resolve API key from explicit param, environment, or ~/.fx/api-key file."""
    if explicit_key and explicit_key.lower() not in ("dummy", "none", "null", "placeholder", "ollama"):
        return explicit_key.strip()
    if "AI_GATEWAY_API_KEY" in os.environ and os.environ["AI_GATEWAY_API_KEY"].strip():
        return os.environ["AI_GATEWAY_API_KEY"].strip()
    if DEFAULT_KEY_PATH.exists():
        try:
            return DEFAULT_KEY_PATH.read_text().strip()
        except Exception:
            pass
    return ""
