# The UTM services, one image, several commands (docker-compose.prod.yml).
# P0-09.
#
# Stage 1 builds the operator console; stage 2 is Python with the project's
# dependencies and the source. Services run from the source tree in
# /app (python -m <package>), the same way they run on a developer's machine,
# so there is one way to start them.

FROM node:20-bookworm-slim AS console
WORKDIR /web-pilot
COPY web-pilot/package.json web-pilot/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web-pilot/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml ./
COPY common/ common/
COPY agent/ agent/
COPY gateway/ gateway/
COPY api/ api/
COPY airspace/ airspace/
RUN pip install . && rm -rf build *.egg-info
COPY tools/ tools/
COPY infra/migrations/ infra/migrations/
COPY --from=console /web-pilot/dist web-pilot/dist
# Never root: nothing here needs it.
RUN useradd --system --uid 10001 --home /app courier \
    && mkdir -p /data/archive && chown -R courier /data
USER courier
