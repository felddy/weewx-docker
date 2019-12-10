ARG GIT_COMMIT=unspecified
ARG GIT_REMOTE=unspecified
ARG VERSION=unspecified

FROM --platform=$TARGETPLATFORM python:2.7-alpine

ARG GIT_COMMIT
ARG GIT_REMOTE
ARG TARGETPLATFORM
ARG VERSION

LABEL org.opencontainers.image.authors="markf+github@geekpad.com"
LABEL org.opencontainers.image.licenses="CC0-1.0"
LABEL org.opencontainers.image.revision=${GIT_COMMIT}
LABEL org.opencontainers.image.source=${GIT_REMOTE}
LABEL org.opencontainers.image.title="WeeWX"
LABEL org.opencontainers.image.vendor="Geekpad"
LABEL org.opencontainers.image.version=${VERSION}

ARG WEEWX_UID=421
ENV WEEWX_HOME="/home/weewx"
ENV WEEWX_VERSION="3.9.2"
ENV ARCHIVE="weewx-${WEEWX_VERSION}.tar.gz"

RUN addgroup --system --gid ${WEEWX_UID} weewx \
  && adduser --system --uid ${WEEWX_UID} --ingroup weewx weewx

RUN apk --update --no-cache add su-exec tar tzdata

WORKDIR ${WEEWX_HOME}

COPY src/entrypoint.sh src/hashes src/version.txt requirements.txt ./

RUN pip install --no-cache --requirement requirements.txt && \
    wget -O "${ARCHIVE}" "http://www.weewx.com/downloads/released_versions/${ARCHIVE}" && \
    sha256sum -c < hashes && \
    tar --extract --gunzip --directory ${WEEWX_HOME} --strip-components=1 --file "${ARCHIVE}" && \
    rm "${ARCHIVE}" && \
    chown -R weewx:weewx . && \
    mkdir /data && \
    cp weewx.conf /data

VOLUME ["/data"]

ENTRYPOINT ["./entrypoint.sh"]
CMD ["/data/weewx.conf"]
