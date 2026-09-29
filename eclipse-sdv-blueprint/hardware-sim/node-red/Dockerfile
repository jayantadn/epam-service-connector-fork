# Copyright (c) 2026 Eclipse Foundation.
#
# This program and the accompanying materials are made available under the
# terms of the MIT License which is available at
# https://opensource.org/licenses/MIT.
#
# SPDX-License-Identifier: MIT

FROM node:22-bookworm-slim

# The Zenoh bridge (zenoh_bus_bridge.py) needs Python.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Node dependencies first so this layer is cached across rebuilds.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# Bookworm's system Python is externally managed (PEP 668); this is a
# throwaway container image, so installing directly is fine here.
RUN pip install --no-cache-dir --break-system-packages "eclipse-zenoh>=1.0.0"

COPY settings.js zenoh_bus_bridge.py entrypoint.sh ./
COPY flows ./flows

RUN chmod +x entrypoint.sh

# Bind to all interfaces inside the container; the host maps the ports it
# actually wants to expose.
ENV NODE_RED_HOST=0.0.0.0 \
    BRIDGE_HOST=0.0.0.0 \
    PORT=1880 \
    BRIDGE_PORT=1881 \
    ZENOH_ROUTER=tcp/127.0.0.1:7447

EXPOSE 1880 1881

ENTRYPOINT ["./entrypoint.sh"]
