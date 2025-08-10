# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.13.0
ARG WEEWX_UID=1000
ARG WEEWX_HOME="/home/weewx"

FROM python:${PYTHON_VERSION} AS build-stage

RUN apt-get update && apt-get install -y clang lld

WORKDIR /tmp
RUN \
  --mount=type=cache,mode=0777,target=/var/cache/apt \
  --mount=type=cache,mode=0777,target=/root/.cache/pip <<EOF
apt-get update
python -m pip install --upgrade pip
pip install --upgrade virtualenv
virtualenv /opt/venv
EOF

COPY pyproject.toml README.md requirements.txt ./
COPY src/_version.py ./src/_version.py

# Python setup
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache --requirement requirements.txt

WORKDIR /root

COPY src/entrypoint.sh src/_version.py ./

FROM python:${PYTHON_VERSION}-slim AS final-stage

ARG TARGETPLATFORM
ARG WEEWX_HOME
ARG WEEWX_UID

# For a list of pre-defined annotation keys and value types see:
# https://github.com/opencontainers/image-spec/blob/master/annotations.md
#
# Note: Additional labels are added by the build workflow.
LABEL org.opencontainers.image.authors="markf+github@geekpad.com"
LABEL org.opencontainers.image.vendor="Geekpad"

RUN addgroup --system --gid ${WEEWX_UID} weewx \
  && adduser --system --uid ${WEEWX_UID} --ingroup weewx weewx

RUN apt-get update && apt-get install -y \
    git \
    libusb-1.0-0 \
    locales \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/* \
  && echo "en_US.UTF-8 UTF-8" >> /etc/locale.gen \
  && echo "de_DE.UTF-8 UTF-8" >> /etc/locale.gen \
  && echo "ca_ES.UTF-8 UTF-8" >> /etc/locale.gen \
  && echo "it_IT.UTF-8 UTF-8" >> /etc/locale.gen \
  && echo "fr_FR.UTF-8 UTF-8" >> /etc/locale.gen \
  && echo "es_ES.UTF-8 UTF-8" >> /etc/locale.gen \
  && echo "nl_NL.UTF-8 UTF-8" >> /etc/locale.gen \
  && echo "pt_PT.UTF-8 UTF-8" >> /etc/locale.gen \
  && locale-gen

WORKDIR ${WEEWX_HOME}

COPY --from=build-stage /opt/venv /opt/venv
COPY --from=build-stage /root ${WEEWX_HOME}

RUN mkdir /data \
  && chown -R weewx:weewx /data

VOLUME ["/data"]

ENV PATH="/opt/venv/bin:$PATH"
ENV PIP_TARGET="/data/lib/python/site-packages"
ENV PYTHONPATH="/data/lib/python/site-packages"
USER weewx
ENTRYPOINT ["./entrypoint.sh"]
