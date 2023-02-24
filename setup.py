"""
This is the setup module for the weewx docker project.

Based on:

- https://packaging.python.org/distributing/
- https://github.com/pypa/sampleproject/blob/master/setup.py
- https://blog.ionelmc.ro/2014/05/25/python-packaging/#the-structure
"""

# Standard Python Libraries
from glob import glob
from os.path import basename, splitext

# Third-Party Libraries
from setuptools import find_packages, setup


def readme():
    """Read in and return the contents of the project's README.md file."""
    with open("README.md", encoding="utf-8") as f:
        return f.read()


def package_vars(version_file):
    """Read in and return the variables defined by the version_file."""
    pkg_vars = {}
    with open(version_file) as f:
        exec(f.read(), pkg_vars)  # nosec
    return pkg_vars


setup(
    name="weewx_docker",
    # Versions should comply with PEP440
    version=package_vars("src/_version.py")["__version__"],
    description="weewx_docker python library",
    long_description=readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/felddy",
    # The project's main homepage
    download_url="https://github.com/felddy/weewx-docker",
    # Author details
    author="Mark Feldhousen",
    author_email="markf@geekpad.com",
    license="License :: OSI Approved :: MIT License",
    # See https://pypi.python.org/pypi?%3Aaction=list_classifiers
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Environment :: Web Environment",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Topic :: Scientific/Engineering :: Atmospheric Science",
    ],
    python_requires=">=3.6",
    # What does your project relate to?
    keywords="weewx",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    py_modules=[splitext(basename(path))[0] for path in glob("src/*.py")],
    install_requires=[
        "configobj == 5.0.8",
        "docker-compose == 1.29.2",
        "paho-mqtt == 1.6.1",
        "pyserial == 3.5",
        "pyusb == 1.2.1",
        "semver == 2.13.0",
        "setuptools == 67.3.2",
        "wheel == 0.38.4",
    ],
    extras_require={
        "test": [
            "coverage == 6.5.0",
            "coveralls == 3.3.1",
            "docker == 6.0.1",
            "pre-commit == 3.1.0",
            "pytest == 7.2.1",
            "pytest-cov == 4.0.0",
            "pytest-lazy-fixture == 0.6.3",
        ]
    },
)
