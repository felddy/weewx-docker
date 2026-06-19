#!/usr/bin/env bash

set -o nounset
set -o errexit
set -o pipefail

version=$(./bump-version show)

git tag "v$version" && git push --tags
