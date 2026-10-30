#!/usr/bin/env bash
set -euo pipefail

# Share the git-ignored local runtime environment with a new git worktree,
# avoiding false failures from missing offline HF models or the QC sandbox.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
COMMON_DIR="$(git -C "$WORKTREE_ROOT" rev-parse --path-format=absolute --git-common-dir)"
IFS= read -r -d '' MAIN_ENTRY < <(git --git-dir="$COMMON_DIR" worktree list --porcelain -z)
MAIN_ROOT="${MAIN_ENTRY#worktree }"
ENV_PATHS=(.venv .sam-isolated poc/memory_m0/model-cache)

# Check every source and destination before changing any links.
for relative in "${ENV_PATHS[@]}"; do
  source_path="$MAIN_ROOT/$relative"
  target_path="$WORKTREE_ROOT/$relative"
  if [[ ! -d "$source_path" ]]; then
    printf 'Missing main-worktree environment: %s\n' "$source_path" >&2
    printf 'Prepare .venv in %s, run tools/build-sam-isolated.sh --qc-only --install there, and provide poc/memory_m0/model-cache first.\n' "$MAIN_ROOT" >&2
    exit 1
  fi
  if [[ "$WORKTREE_ROOT" != "$MAIN_ROOT" && -e "$target_path" && ! -L "$target_path" ]]; then
    printf 'Cannot replace existing file or directory: %s (preserved).\n' "$target_path" >&2
    exit 1
  fi
done

if [[ "$WORKTREE_ROOT" == "$MAIN_ROOT" ]]; then
  printf 'Main-worktree environment is present: %s\n' "$MAIN_ROOT"
  exit 0
fi

for relative in "${ENV_PATHS[@]}"; do
  source_path="$MAIN_ROOT/$relative"
  target_path="$WORKTREE_ROOT/$relative"
  mkdir -p -- "$(dirname -- "$target_path")"
  if [[ ! -L "$target_path" || "$(readlink -f -- "$target_path")" != "$(readlink -f -- "$source_path")" ]]; then
    ln -sfnT -- "$source_path" "$target_path"
  fi
  printf 'Ready: %s -> %s\n' "$target_path" "$source_path"
done
