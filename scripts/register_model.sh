#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
container_start
container_exec python -m so101_core.model_registry "$@"
