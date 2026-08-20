import os
from pathlib import Path
from typing import List

__version__ = "0.2.0"

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "https://ai-gateway.vercel.sh/v3/ai/language-model")
USER_AGENT = os.environ.get("FX_USER_AGENT", "fx/0.0.4")
DEFAULT_KEY_PATH = Path.home() / ".fx" / "api-key"
DEFAULT_HOST = os.environ.get("HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PORT", "18080"))

PLACEHOLDER_KEYS = ("dummy", "none", "null", "placeholder", "ollama")

# Adaptive key routing: sliding-window stats, learned limits, exponential-backoff cooldown
MAX_KEY_RETRIES = int(os.environ.get("FX_MAX_KEY_RETRIES", "3"))
KEY_COOLDOWN_BASE = float(os.environ.get("FX_COOLDOWN_BASE", "30"))    # first 429 cooldown (s)
KEY_COOLDOWN_MAX = float(os.environ.get("FX_COOLDOWN_MAX", "300"))     # backoff cap (s)
EST_RPM_LIMIT = float(os.environ.get("FX_LIMIT_RPM", "60"))            # initial per-key RPM estimate
EST_TPM_LIMIT = float(os.environ.get("FX_LIMIT_TPM", "20000"))         # initial per-key TPM estimate
EST_RPM_MAX = float(os.environ.get("FX_LIMIT_RPM_MAX", "600"))         # learned ceiling cap
EST_TPM_MAX = float(os.environ.get("FX_LIMIT_TPM_MAX", "1000000"))     # learned ceiling cap

MODELS = (
    {"id": "zai/glm-5.2", "object": "model", "owned_by": "zai", "permission": []},
    {"id": "zai/glm-5.2-fast", "object": "model", "owned_by": "zai", "permission": []},
)


def resolve_keys(explicit_key: str = "") -> List[str]:
    """Resolve API keys from explicit param, env (comma/newline separated multi-key), or ~/.fx/api-key file."""
    if explicit_key and explicit_key.lower() not in PLACEHOLDER_KEYS:
        return [explicit_key.strip()]

    def split_keys(raw: str) -> List[str]:
        return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]

    keys: List[str] = []
    for env_name in ("AI_GATEWAY_API_KEYS", "AI_GATEWAY_API_KEY"):
        raw = os.environ.get(env_name, "")
        if raw.strip():
            keys.extend(split_keys(raw))
    if not keys and DEFAULT_KEY_PATH.exists():
        try:
            keys.extend(split_keys(DEFAULT_KEY_PATH.read_text()))
        except Exception:
            pass
    # Dedup while preserving order (same key in both env vars shouldn't double-count).
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def mask_key(key: str) -> str:
    """Partial key for logs and stats: abcd...wxyz"""
    if len(key) <= 8:
        return key[:2] + "***"
    return key[:7] + "..." + key[-4:]
