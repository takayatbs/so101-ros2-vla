#!/usr/bin/env bash
# Install model extras in an isolated venv; keep the running ROS venv intact.
source "$(dirname "$0")/common.sh"
TASK_POLICY="${1:?Usage: install_policies.sh MODEL (or all)}"
case "$TASK_POLICY" in
  act) TASK_EXTRAS="" ;;
  diffusion) TASK_EXTRAS="diffusion" ;;
  smolvla) TASK_EXTRAS="smolvla" ;;
  pi0|pi05|pi0_fast) TASK_EXTRAS="pi" ;;
  groot) TASK_EXTRAS="groot" ;;
  xvla) TASK_EXTRAS="xvla" ;;
  all) TASK_EXTRAS="diffusion,smolvla,pi,groot,xvla" ;;
  *) echo 'Unknown model. Choose act, diffusion, smolvla, pi0, pi05, pi0_fast, groot, xvla, all.' >&2; exit 2 ;;
esac
container_start
# Source acquisition and installation both run inside the project and lock.
container_exec flock -n -E 75 /workspace/data/.compute.lock python /workspace/scripts/install_policy_env.py "$TASK_EXTRAS"
