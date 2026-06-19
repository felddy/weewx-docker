# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.14.6
ARG WEEWX_UID=1000
ARG WEEWX_HOME="/home/weewx"

FROM python:${PYTHON_VERSION} AS build-stage

# clang/lld are used to build any dependencies that ship only as source
# distributions on the less common target platforms.
RUN apt-get update && apt-get install -y clang lld

# Create the virtual environment that will be copied into the final stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /tmp/build

# Copy only the files needed to build and install the project. The dynamic
# project version is read from src/version.txt, so it must be present.
COPY pyproject.toml README.md ./
COPY src/version.txt ./src/version.txt

# Install the project and its runtime dependencies (weewx, etc.) into /opt/venv.
# pip is used here (rather than uv) because the image is built for many
# architectures -- including linux/arm/v6, linux/arm/v7, linux/riscv64,
# linux/ppc64le, and linux/s390x -- for which uv does not publish binaries.
RUN pip install --no-cache-dir --upgrade pip \
  && pip install --no-cache-dir .

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

# libnss-wrapper lets the entrypoint synthesize passwd/group entries at runtime
# so the container works under an arbitrary uid:gid (rootless / Kubernetes
# runAsUser) without write access to /etc/passwd. See src/entrypoint.sh.
RUN apt-get update && apt-get install -y git libusb-1.0-0 libtiff6 libopenjp2-7 libfreetype6 libnss-wrapper

WORKDIR ${WEEWX_HOME}

COPY --from=build-stage /opt/venv /opt/venv
COPY src/entrypoint.sh ./

# /data is owned by weewx but made world-writable with the sticky bit (1777,
# like /tmp): the container may run under an arbitrary uid:gid (rootless /
# Kubernetes), and the sticky bit still prevents users from removing files
# they do not own. A mounted volume's own permissions take precedence at run
# time; this only governs the bare image directory.
RUN echo "${CONTAINER_VERSION}" > image_version.txt \
  && chmod a+rx entrypoint.sh \
  && chmod a+rx "${WEEWX_HOME}" \
  && mkdir /data \
  && chown -R weewx:weewx /data \
  && chmod 1777 /data

VOLUME ["/data"]

# Default to the non-root "weewx" user (uid/gid 1000). The container also
# supports being run under an arbitrary uid:gid; see src/entrypoint.sh.
ENV HOME="${WEEWX_HOME}"
ENV PATH="/opt/venv/bin:$PATH"
ENV PIP_TARGET="/data/lib/python/site-packages"
ENV PYTHONPATH="/data/lib/python/site-packages"
USER weewx
ENTRYPOINT ["./entrypoint.sh"]
