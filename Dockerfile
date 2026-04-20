FROM python:3.13-slim-bookworm AS tilemaker-builder

ARG TILEMAKER_VERSION=v3.0.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        git \
        libboost-dev \
        libboost-filesystem-dev \
        libboost-iostreams-dev \
        libboost-program-options-dev \
        libboost-system-dev \
        liblua5.4-dev \
        libshp-dev \
        libsqlite3-dev \
        lua5.4 \
        rapidjson-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp

RUN git clone --depth 1 --branch ${TILEMAKER_VERSION} https://github.com/systemed/tilemaker.git

WORKDIR /tmp/tilemaker

RUN make \
    && make install \
    && install -d /opt/tilemaker \
    && cp config.json process.lua /opt/tilemaker/ \
    && if [ -d resources ]; then cp -r resources /opt/tilemaker/resources; fi


FROM python:3.13-slim-bookworm AS runtime

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TILEMAKER_BINARY=/usr/local/bin/tilemaker \
    TILEMAKER_CONFIG=/opt/tilemaker/config.json \
    TILEMAKER_PROCESS=/opt/tilemaker/process.lua \
    MAPS_ROOT=/osm_maps

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        gosu \
        libboost-filesystem1.74.0 \
        libboost-iostreams1.74.0 \
        libboost-program-options1.74.0 \
        libboost-system1.74.0 \
        liblua5.4-0 \
        libshp2 \
        libsqlite3-0 \
        osmium-tool \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN pip install -r /app/requirements.txt

COPY app /app/app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=tilemaker-builder /usr/local/bin/tilemaker /usr/local/bin/tilemaker
COPY --from=tilemaker-builder /usr/local/bin/tilemaker-server /usr/local/bin/tilemaker-server
COPY --from=tilemaker-builder /opt/tilemaker /opt/tilemaker

RUN groupadd --gid ${APP_GID} appuser \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --create-home --home-dir /home/appuser appuser \
    && mkdir -p /osm_maps \
    && chown -R appuser:appuser /app /osm_maps \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["hypercorn", "app.api:app", "--bind", "0.0.0.0:3502", "--workers", "2"]
