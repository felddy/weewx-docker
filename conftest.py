"""Root pytest configuration.

``pytest_addoption`` must live in an initial conftest (at or above the rootdir)
so options register before the command line is parsed. See:
https://docs.pytest.org/en/stable/reference/reference.html#pytest.hookspec.pytest_addoption
"""


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
