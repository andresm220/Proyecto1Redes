# The netops MCP server, served over Streamable HTTP.
#
# The image carries the whole package, not just servers/netops/: core.py
# imports the hand-written JSON-RPC error types from host/mcp/jsonrpc.py, so
# the server and the host share one definition of what a -32602 is rather than
# each keeping its own copy.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

# Dependencies first, so a code change does not re-resolve the whole tree.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY host/ ./host/
COPY servers/ ./servers/

# The seed ships read-only inside the image; the mutable state is written at
# runtime. Splitting them is what lets this run where only /tmp is writable.
ENV NETOPS_SEED_DIR=/app/servers/netops/data/seed \
    NETOPS_DATA_DIR=/tmp/netops-data

# Cloud Run injects PORT and ignores EXPOSE, but stating it keeps `docker run`
# honest for anyone reading this file.
ENV PORT=8080
EXPOSE 8080

# Shell form, so ${PORT} is expanded at start-up rather than taken literally.
#
# Docker's linter warns that shell form can swallow OS signals. `exec` is what
# answers it: the shell replaces itself with uvicorn, so uvicorn runs as PID 1
# and SIGTERM reaches it directly. Verified - `docker stop` returns in ~2s with
# "Application shutdown complete" in the log, not after the 10s kill timeout.
CMD exec uvicorn servers.netops.http_server:app --host 0.0.0.0 --port ${PORT}
