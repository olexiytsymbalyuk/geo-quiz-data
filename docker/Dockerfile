# Образ інструментів конвеєра: osmium-tool (apt) і tippecanoe (збірка за пінованим тегом).
# Збирається нативно на arm64 (Mac) і на linux/amd64 (GitHub Actions) — платформа не хардкодиться.

FROM debian:bookworm-slim AS tippecanoe-build
ARG TIPPECANOE_VERSION=2.79.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates git libsqlite3-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${TIPPECANOE_VERSION}" https://github.com/felt/tippecanoe.git /src \
    && make -C /src -j"$(nproc)" \
    && make -C /src install PREFIX=/opt/tippecanoe

FROM debian:bookworm-slim
ARG TIPPECANOE_VERSION=2.79.0
ARG OSMIUM_VERSION=1.15.0-1
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        osmium-tool=${OSMIUM_VERSION} libsqlite3-0 zlib1g libstdc++6 time \
    && rm -rf /var/lib/apt/lists/*
COPY --from=tippecanoe-build /opt/tippecanoe/bin/tippecanoe /opt/tippecanoe/bin/tile-join /opt/tippecanoe/bin/tippecanoe-decode /usr/local/bin/
LABEL org.geo-quiz.osmium-version="${OSMIUM_VERSION}"
LABEL org.geo-quiz.tippecanoe-version="${TIPPECANOE_VERSION}"
WORKDIR /work
