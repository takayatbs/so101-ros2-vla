#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
container_start
container_exec flock -n -E 75 /workspace/data/.compute.lock \
  env "RUN_ML_TESTS=${RUN_ML_TESTS:-1}" /workspace/data/envs/policies/bin/python -m pytest \
  /workspace/tests/test_policy_catalog.py \
  /workspace/tests/test_async_chunks.py \
  /workspace/tests/test_action_smoothing.py \
  /workspace/tests/test_ml.py \
  /workspace/tests/test_lerobot_models.py \
  /workspace/tests/test_lerobot_flow.py \
  /workspace/tests/test_extended_flow.py \
  /workspace/tests/test_methods.py \
  /workspace/tests/test_methods_integration.py "$@"
