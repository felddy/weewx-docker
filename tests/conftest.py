"""pytest configuration."""

# Standard Python Libraries
import os
from pathlib import Path
import re
from typing import List

# Third-Party Libraries
import docker
import pytest

from .utils import RedactedPrinter

MAIN_SERVICE_NAME = "weewx"
REDACTION_REGEXES: List[re.Pattern] = []
VERSION_FILE = "src/_version.py"
VERSION_SERVICE_NAME = f"{MAIN_SERVICE_NAME}-version"
GEN_TEST_CONFIG_SERVICE_NAME = f"{MAIN_SERVICE_NAME}-gen-test-config"

client = docker.from_env()


@pytest.fixture(autouse=True)
def group_github_log_lines(request):
    """Group log lines when running in GitHub actions."""
    # Group output from each test with workflow log groups
    # https://help.github.com/en/actions/reference/workflow-commands-for-github-actions#grouping-log-lines

    if os.environ.get("GITHUB_ACTIONS") != "true":
        # Not running in GitHub actions
        yield
        return
    # Group using the current test name
    print()
    print(f"::group::{request.node.name}")
    yield
    print()
    print("::endgroup::")


@pytest.fixture(scope="session")
def gen_test_config_container(image_tag):
    """Fixture for the test configuration generator container."""
    container = client.containers.run(
        image_tag,
        command="--gen-test-config",
        detach=True,
        name=GEN_TEST_CONFIG_SERVICE_NAME,
        volumes={str(Path.cwd() / Path("data")): {"bind": "/data", "driver": "local"}},
    )
    yield container
    container.remove(force=True)


@pytest.fixture(scope="session")
def main_container(image_tag):
    """Fixture for the main weewx container."""
    container = client.containers.run(
        image_tag,
        detach=True,
        environment={
            "WEEWX_GID": "dialout",
            "WEEWX_UID": "weewx",
            "TIMEZONE": "UTC",
        },
        name=MAIN_SERVICE_NAME,
        ports={},
        volumes={str(Path.cwd() / Path("data")): {"bind": "/data", "driver": "local"}},
    )
    yield container
    container.remove(force=True)


@pytest.fixture(scope="session")
def version_container(image_tag):
    """Fixture for the version container."""
    container = client.containers.run(
        image_tag,
        command="--version",
        detach=True,
        name=VERSION_SERVICE_NAME,
    )
    yield container
    container.remove(force=True)


@pytest.fixture(scope="session")
def project_version():
    """Get the project version."""
    pkg_vars = {}
    with open(VERSION_FILE) as f:
        exec(f.read(), pkg_vars)  # nosec
    return pkg_vars["__version__"]


@pytest.fixture(scope="session")
def redacted_printer():
    """Return a configured redacted printer object."""
    return RedactedPrinter(REDACTION_REGEXES)


def pytest_addoption(parser):
    """Add new commandline options to pytest."""
    parser.addoption(
        "--runslow", action="store_true", default=False, help="run slow tests"
    )
    parser.addoption(
        "--image-tag",
        action="store",
        default="local/test-image:latest",
        help="image tag to test",
    )


@pytest.fixture(scope="session")
def image_tag(request):
    """Get the image tag to test."""
    return request.config.getoption("--image-tag")


def pytest_collection_modifyitems(config, items):
    """Modify collected tests based on custom marks and commandline options."""
    if config.getoption("--runslow"):
        # --runslow given in cli: do not skip slow tests
        return
    skip_slow = pytest.mark.skip(reason="need --runslow option to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
