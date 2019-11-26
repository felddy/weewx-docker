ARG GIT_COMMIT=unspecified
ARG GIT_REMOTE=unspecified
ARG VERSION=unspecified

FROM python:2.7-alpine

ARG GIT_COMMIT
ARG GIT_REMOTE
ARG VERSION

LABEL git_commit=${GIT_COMMIT}
LABEL git_remote=${GIT_REMOTE}
LABEL maintainer="markf+github@geekpad.com"
LABEL vendor="Geekpad"
LABEL version=${VERSION}

ARG WEEWX_UID=421
ENV WEEWX_HOME="/home/weewx"
ENV WEEWX_VERSION="3.9.2"
ENV ARCHIVE="weewx-${WEEWX_VERSION}.tar.gz"

RUN addgroup --system --gid ${WEEWX_UID} weewx \
  && adduser --system --uid ${WEEWX_UID} --ingroup weewx weewx

RUN apk --update --no-cache add su-exec tar

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
