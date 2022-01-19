# weewx-docker 🌩🐳 #

[![GitHub Build Status](https://github.com/felddy/weewx-docker/workflows/build/badge.svg)](https://github.com/felddy/weewx-docker/actions)
[![CodeQL](https://github.com/felddy/weewx-docker/workflows/CodeQL/badge.svg)](https://github.com/felddy/weewx-docker/actions/workflows/codeql-analysis.yml)
[![WeeWX Version](https://img.shields.io/github/v/release/felddy/weewx-docker?color=brightgreen)](https://hub.docker.com/r/felddy/weewx)

[![Docker Pulls](https://img.shields.io/docker/pulls/felddy/weewx)](https://hub.docker.com/r/felddy/weewx)
[![Docker Image Size (latest by date)](https://img.shields.io/docker/image-size/felddy/weewx)](https://hub.docker.com/r/felddy/weewx)
[![Platforms](https://img.shields.io/badge/platforms-amd64%20%7C%20arm%2Fv6%20%7C%20arm%2Fv7%20%7C%20arm64%20%7C%20ppc64le%20%7C%20s390x-blue)](https://hub.docker.com/r/felddy/weewx/tags)

This docker container can be used to quickly get a
[WeeWX](http://weewx.com) instance up and running.

This container has the following WeeWX extensions installed:

- [interceptor](https://github.com/matthewwall/weewx-interceptor)
- [mqtt](https://github.com/weewx/weewx/wiki/mqtt)

## Running ##

### Running with Docker ###

Pull `felddy/weewx` from the Docker repository:

```console
docker pull felddy/weewx
```

### Run ###

The easiest way to start the container is to create a
`docker-compose.yml` similar to the following.  If you use a
serial port to connect to your weather station, make sure the
container has permissions to access the port.  The uid/gid can
be set using the environment variables below.

Modify any paths or devices as needed:

```yaml
---
version: "3.8"

volumes:
  data:

services:
  weewx:
    image: felddy/weewx
    init: true
    restart: "yes"
    volumes:
      - type: bind
        source: ./data
        target: /data
    environment:
      - TIMEZONE=US/Eastern
      - WEEWX_UID=weewx
      - WEEWX_GID=dialout
    devices:
      - "/dev/ttyUSB0:/dev/ttyUSB0"
```

1. Create a directory on the host to store the configuration and database files:

    ```console
    mkdir data
    ```

1. If this is the first time running weewx, use the following command to start
   the container and generate a configuration file:

    ```console
    docker-compose run weewx
    ```

1. The configuration file will be created in the `data` directory.  You should
   edit this file to match the setup of your weather station.

1. When you are satisfied with configuration the container can be started in the
   background with:

    ```console
    docker-compose up -d
    ```

## Upgrading ##

1. Stop the running container:

    ```console
    docker-compose down
    ```

1. Pull the new images from the Docker hub:

    ```console
    docker-compose pull
    ```

1. Update your configuration file (a backup will be created):

    ```console
    docker-compose run weewx --upgrade
    ```

1. Read through the new configuration and verify.
   It is helpful to `diff` the new config with the backup.  Check the
   [WeeWX Upgrade Guide](http://weewx.com/docs/upgrading.htm#Instructions_for_specific_versions)
   for instructions for specific versions.

1. Start the container up with the new image version:

    ```console
    docker-compose up -d
    ```

## Volumes ##

| Mount point | Purpose        |
|-------------|----------------|
| `/data`     | configuration file and sqlite database storage |

## Environment Variables ##

| Variable       | Purpose | Default |
|----------------|---------|---------|
| `TIMEZONE`     | Container [TZ database name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones#List) | `UTC` |
| `WEEWX_UID`    | `uid` the daemon will be run under | `weewx` |
| `WEEWX_GID`    | `gid` the deamon will be run under | `weewx` |

## Building from source ##

Build the image locally using this git repository as the [build context](https://docs.docker.com/engine/reference/commandline/build/#git-repositories):

```console
docker build \
  --tag felddy/weewx:4.5.1 \
  https://github.com/felddy/weewx-docker.git#develop
```

## Cross-platform builds ##

To create images that are compatible with other platforms you can use the
[`buildx`](https://docs.docker.com/buildx/working-with-buildx/) feature of
Docker:

1. Copy the project to your machine using the `Clone` button above
   or the command line:

    ```console
    git clone https://github.com/felddy/weewx-docker.git
    cd weewx-docker
    ```

1. Create the `Dockerfile-x` file with `buildx` platform support:

    ```console
    ./buildx-dockerfile.sh
    ```

1. Build the image using `buildx`:

    ```console
    docker buildx build \
      --file Dockerfile-x \
      --platform linux/amd64 \
      --output type=docker \
      --tag felddy/weewx:4.5.1 .
    ```

## Debugging ##

There are a few helper arguments that can be used to diagnose container issues
in your environment.

| Purpose | Command |
|---------|---------|
| Generate the default configuration | `docker-compose run weewx` |
| Upgrade a previous configuration | `docker-compose run weewx --upgrade` |
| Generate a test (simulator) configuration | `docker-compose run weewx --gen-test-config` |
| Drop into a shell in the container | `docker-compose run weewx --shell` |

## New repositories from a skeleton ##

Please see our [Project Setup guide](https://github.com/cisagov/development-guide/tree/develop/project_setup)
for step-by-step instructions on how to start a new repository from
a skeleton. This will save you time and effort when configuring a
new repository!

## Contributing ##

We welcome contributions!  Please see [`CONTRIBUTING.md`](CONTRIBUTING.md) for
details.

## License ##

This project is in the worldwide [public domain](LICENSE).

This project is in the public domain within the United States, and
copyright and related rights in the work worldwide are waived through
the [CC0 1.0 Universal public domain
dedication](https://creativecommons.org/publicdomain/zero/1.0/).

All contributions to this project will be released under the CC0
dedication. By submitting a pull request, you are agreeing to comply
with this waiver of copyright interest.
