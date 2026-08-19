FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy project files
COPY pyproject.toml README.md LICENSE ./
COPY fx_gateway_proxy ./fx_gateway_proxy

# Install dependencies and project in editable mode
RUN uv pip install --no-cache --system -e .

ENV HOST=0.0.0.0
ENV PORT=18080

EXPOSE 18080

CMD ["python", "-m", "fx_gateway_proxy.cli", "--host", "0.0.0.0", "--port", "18080"]
