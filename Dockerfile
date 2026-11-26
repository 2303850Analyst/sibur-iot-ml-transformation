ARG PYTHON_IMAGE=python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
FROM ${PYTHON_IMAGE} AS base
WORKDIR /lab
COPY requirements.lock.txt /tmp/requirements.lock.txt

FROM base AS gateway
RUN pip install --no-cache-dir -c /tmp/requirements.lock.txt paho-mqtt==2.1.0 requests==2.32.4

FROM base AS runner
RUN pip install --no-cache-dir -r /tmp/requirements.lock.txt
