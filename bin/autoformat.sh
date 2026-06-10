#!/bin/bash -ex

THIS_DIR=$(dirname "${BASH_SOURCE[0]}")
PROJECT_ROOT=$(realpath "${THIS_DIR}/..")
cd "${PROJECT_ROOT}"
pdm run ruff format src tests
pdm run ruff check src tests --fix