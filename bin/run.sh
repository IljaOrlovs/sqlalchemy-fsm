#!/bin/bash -xe

THIS_DIR=$(dirname "${BASH_SOURCE[0]}")
PROJECT_ROOT=$(realpath "${THIS_DIR}/..")
cd "${PROJECT_ROOT}"

if [ -n "$*" ]; then
    ARGS=("$@")
else
    # ARGS=(python -m fca_mcp serve --reload)
     ARGS=("$@")
fi

cd "${THIS_DIR}/.."
exec op run --no-masking -- pdm run "${ARGS[@]}"