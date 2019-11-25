# weewx-docker 🌩🐳 #

[![GitHub Build Status](https://github.com/felddy/weewx-docker/workflows/build/badge.svg)](https://github.com/felddy/weewx-docker/actions)
[![Total alerts](https://img.shields.io/lgtm/alerts/g/felddy/weewx-docker.svg?logo=lgtm&logoWidth=18)](https://lgtm.com/projects/g/felddy/weewx-docker/alerts/)
[![Language grade: Python](https://img.shields.io/lgtm/grade/python/g/felddy/weewx-docker.svg?logo=lgtm&logoWidth=18)](https://lgtm.com/projects/g/felddy/weewx-docker/context:python)

## Docker Image ##

![MicroBadger Layers](https://img.shields.io/microbadger/layers/felddy/weewx.svg)
![MicroBadger Size](https://img.shields.io/microbadger/image-size/felddy/weewx.svg)

This docker container can be used to quickly get a
[WeeWX](http://weewx.com) instance up and running.

## Usage ##

### Install ###

Pull `felddy/weewx` from the Docker repository:

```console
docker pull felddy/weewx
```

Or build `felddy/weewx` from source:

```console
git clone https://github.com/felddy/weewx-docker.git
cd weewx-docker
docker-compose build --build-arg VERSION=3.9.2
```

### Run ###

The easiest way to start the container is to create a
`docker-compose.yml` similar to the following.  Modify any
paths or devices as needed:

```yaml
---
version: "3.7"

volumes:
  data:

services:
  weewx:
    image: felddy/weewx
    init: true
    restart: "no"
    volumes:
      - type: bind
        source: ./data
        target: /data
    devices:
      - "/dev/ttyUSB0:/dev/ttyUSB0"
```

Create a directory on the host to store the configuration and database files:

```console
mkdir data
```

If this is the first time running weewx, use the following command to
start the container and generate a configuration file:

```console
docker-compose run weewx
```

The configuration file will be created in the `data` directory.
You should edit this file to match the setup of your weather station.
When you are satisfied with configuration the container can be started
in the background with:

```console
docker-compose up -d
```

## Volumes ##

| Mount point | Purpose        |
|-------------|----------------|
| /data    | configuration file and sqlite database storage |

## Contributing ##

We welcome contributions!  Please see [here](CONTRIBUTING.md) for
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
