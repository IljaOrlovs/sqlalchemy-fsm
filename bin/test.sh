#!/bin/bash -e

THIS_DIR=$(dirname "${BASH_SOURCE[0]}")
PROJECT_ROOT=$(realpath "${THIS_DIR}/..")
cd "${PROJECT_ROOT}"

export FCA_API_USERNAME="mock-api-username"
export FCA_API_KEY="mock-api-key"

exec op run --no-masking --  pdm run pytest \
    --cache-clear \
    --capture=no \
    --code-highlight=yes \
    --color=yes \
    --cov=src \
    --cov-report=term-missing:skip-covered \
    -ra \
    --no-cov-on-fail \
    --tb=native \
    --verbosity=3 \
    "${@:-tests/}"