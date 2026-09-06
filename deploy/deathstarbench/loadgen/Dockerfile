# Build from a verified, immutable DeathStarBench commit:
#   docker build \
#     --build-arg DEATHSTARBENCH_COMMIT=<40-character commit SHA> \
#     -f deploy/deathstarbench/loadgen/Dockerfile \
#     -t <registry>/dsb-tools:<tag> .
#
# The image contains the upstream wrk2 binary and its Social Network seeding
# script. It deliberately has no default commit: the experiment manifest must
# record the exact source revision used in a run.
FROM ubuntu:22.04

ARG DEATHSTARBENCH_REPOSITORY="https://github.com/delimitrou/DeathStarBench.git"
ARG DEATHSTARBENCH_COMMIT

ENV DEBIAN_FRONTEND=noninteractive

RUN test -n "${DEATHSTARBENCH_COMMIT}" \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential ca-certificates git libssl-dev libz-dev \
      lua-socket luarocks luajit python3 python3-aiohttp \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --recurse-submodules "${DEATHSTARBENCH_REPOSITORY}" /opt/deathstarbench \
    && cd /opt/deathstarbench \
    && git checkout --detach "${DEATHSTARBENCH_COMMIT}" \
    && git submodule update --init --recursive \
    && make -C /opt/deathstarbench/wrk2

WORKDIR /opt/deathstarbench
