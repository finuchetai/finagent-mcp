FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir '.[files,postgres,remote]' && useradd --uid 10001 --create-home finance
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "finance_mcp.server", "--config", "/data/config.json", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
