FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy project definition and install dependencies
COPY pyproject.toml .
RUN uv pip install --no-cache --system -e .

# Copy application source
COPY fx_gateway_proxy ./fx_gateway_proxy

ENV HOST=0.0.0.0
ENV PORT=18080

EXPOSE 18080

CMD ["python", "-m", "fx_gateway_proxy.cli", "--host", "0.0.0.0", "--port", "18080"]
