#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
container_start
container_exec flock -n -E 75 /workspace/data/.compute.lock python /workspace/training/train.py "$@"
