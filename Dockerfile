# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.13.11
ARG UV_VERSION=0.9
ARG WEEWX_UID=1000
ARG WEEWX_HOME="/home/weewx"

# uv binary source stage
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-stage

FROM python:${PYTHON_VERSION} AS build-stage

# Bring in the uv binary from the pinned uv image.
COPY --from=uv-stage /uv /uvx /bin/

RUN apt-get update && apt-get install -y clang lld

# uv configuration:
#   - place the managed virtual environment at a well-known path
#   - compile bytecode for faster container start-up
#   - copy (rather than hardlink) packages so the venv is self-contained
#   - use the image's system Python (never download a managed interpreter) so
#     the resulting /opt/venv is valid when copied into the slim final stage
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_PYTHON=/usr/local/bin/python3
ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Copy only the files needed to resolve and install the dependencies. The
# dynamic project version is read from src/version.txt, so it must be present.
COPY pyproject.toml uv.lock README.md ./
COPY src/version.txt ./src/version.txt

# Install only the runtime dependencies (no dev/test groups, no editable
# install of the project itself) into /opt/venv.
RUN uv sync --frozen --no-default-groups --no-install-project

FROM python:${PYTHON_VERSION}-slim AS final-stage

ARG CONTAINER_VERSION
ARG TARGETPLATFORM
ARG WEEWX_HOME
ARG WEEWX_UID

RUN test -n "${CONTAINER_VERSION}" || { echo "ERROR: CONTAINER_VERSION must be passed as --build-arg"; exit 1; }

# For a list of pre-defined annotation keys and value types see:
# https://github.com/opencontainers/image-spec/blob/master/annotations.md
#
# Note: Additional labels are added by the build workflow.
LABEL org.opencontainers.image.authors="markf+github@geekpad.com"
LABEL org.opencontainers.image.vendor="Geekpad"

RUN addgroup --system --gid ${WEEWX_UID} weewx \
  && adduser --system --uid ${WEEWX_UID} --ingroup weewx weewx

RUN apt-get update && apt-get install -y git libusb-1.0-0 libtiff6 libopenjp2-7 libfreetype6

WORKDIR ${WEEWX_HOME}

COPY --from=build-stage /opt/venv /opt/venv
COPY src/entrypoint.sh ./

RUN echo "${CONTAINER_VERSION}" > image_version.txt \
  && chmod a+rx entrypoint.sh \
  && mkdir /data \
  && chown -R weewx:weewx /data

VOLUME ["/data"]

ENV PATH="/opt/venv/bin:$PATH"
ENV PIP_TARGET="/data/lib/python/site-packages"
ENV PYTHONPATH="/data/lib/python/site-packages"
USER weewx
ENTRYPOINT ["./entrypoint.sh"]
