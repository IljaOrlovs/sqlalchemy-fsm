#!/bin/bash -xe

PROJECT_ROOT_DIR=$(dirname "${BASH_SOURCE[0]}")/..

cd "${PROJECT_ROOT_DIR}"

if [ $# -eq 0 ]; then
    ARGS=( src/ )
else
    ARGS=( "$@" )
fi

pdm run pyright "${ARGS[@]}"
