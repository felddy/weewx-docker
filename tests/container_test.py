#!/usr/bin/env pytest -vs
"""Tests for example container."""

# Standard Python Libraries
import os
import time

# Third-Party Libraries
import pytest

READY_MESSAGE = "engine: Starting main packet loop"
VERSION_FILE = "src/_version.py"


def test_gen_config(gen_test_config_container):
    """Test that the test configuration generator has completed."""
    # Wait until the container has exited or timeout.

    for _ in range(10):
        gen_test_config_container.reload()
        if gen_test_config_container.status == "exited":
            break
        time.sleep(1)
    assert gen_test_config_container.status in ("exited")


@pytest.mark.parametrize(
    "container",
    [
        "main_container",
        "version_container",
    ],
)
def test_container_running(container, request):
    """Test that the container has started."""
    # Lazy fixture evaluation
    container = request.getfixturevalue(container)

    # Wait until the container is running or timeout.
    for _ in range(10):
        container.reload()
        if container.status != "created":
            break
        time.sleep(1)
    assert container.status in ("exited", "running")


def test_wait_for_version_container_exit(version_container):
    """Wait for version container to exit cleanly."""
    assert (
        version_container.wait()["StatusCode"] == 0
    ), "The version container did not exit cleanly"


def test_log_version(version_container, project_version):
    """Verify the container outputs the correct version to the logs."""
    version_container.wait()  # make sure container exited if running test isolated
    log_output = version_container.logs().decode("utf-8").strip()
    assert (
        log_output == project_version
    ), "Container version output to log does not match project version file"


def test_wait_for_ready(main_container):
    """Wait for container to be ready."""
    TIMEOUT = 10
    for _ in range(TIMEOUT):
        if READY_MESSAGE in main_container.logs().decode("utf-8"):
            break
        time.sleep(1)
    else:
        raise Exception(
            f"Container does not seem ready.  "
            f'Expected "{READY_MESSAGE}" in the log within {TIMEOUT} seconds.'
        )


@pytest.mark.skipif(
    os.environ.get("RELEASE_TAG") in [None, ""],
    reason="this is not a release (RELEASE_TAG not set)",
)
def test_release_version(project_version):
    """Verify that release tag version agrees with the module version."""
    assert (
        os.getenv("RELEASE_TAG") == f"v{project_version}"
    ), "RELEASE_TAG does not match the project version"


# The container version label is added during the GitHub Actions build workflow.
# It will not be present if the container is built locally.
# Skip this check if we are not running in GitHub Actions.
@pytest.mark.skipif(
    os.environ.get("GITHUB_ACTIONS") != "true", reason="not running in GitHub Actions"
)
def test_container_version_label_matches(version_container, project_version):
    """Verify the container version label is the correct version."""
    assert (
        version_container.labels["org.opencontainers.image.version"] == project_version
    ), "Container version label does not match project version"
