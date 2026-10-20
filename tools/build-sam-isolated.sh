#!/usr/bin/env bash
set -euo pipefail

# Build the persistent SAM workspace. This script does not enable networking.
# Dependency installation is deliberately separate: run with --install only
# from a network-authorized execution channel.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ISO_ROOT="${SAM_ISOLATED_ROOT:-$SOURCE_ROOT/.sam-isolated}"
RESET=0
INSTALL=0
QC_ONLY=0

usage() {
  cat <<EOF
Usage: $0 [--reset] [--install] [--qc-only]

  --qc-only  Prepare/install parser + pinned quality requirements only; no checkout/data copy.
  --reset    Recreate the isolated workspace and venv directories.
  --install  Install requirements (requires an explicitly network-enabled run).

Default mode only prepares directories and copies project files; it never
opens networking or contacts package indexes.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --reset) RESET=1 ;;
    --install) INSTALL=1 ;;
    --qc-only) QC_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

mkdir -p "$ISO_ROOT"/{rootfs,workspace,data,venv,logs,fonts}
# Local installed fonts only, never downloaded or copied into the repository.
# Rendering needs the original Word font family, not an invented substitution.
FONT_SOURCE="${SAM_QC_FONT_SOURCE:-/mnt/c/Windows/Fonts}"
for font in "$FONT_SOURCE"/msyh*.ttc "$FONT_SOURCE"/simsun*.tt* "$FONT_SOURCE"/simhei.ttf; do
  if [[ -f "$font" ]]; then cp "$font" "$ISO_ROOT/fonts/"; fi
done

if [[ "$RESET" == 1 ]]; then
  rm -rf "$ISO_ROOT/workspace" "$ISO_ROOT/venv" "$ISO_ROOT/logs"
  mkdir -p "$ISO_ROOT/workspace" "$ISO_ROOT/venv" "$ISO_ROOT/logs"
fi

if [[ "$QC_ONLY" == 1 ]]; then
  cp "$SOURCE_ROOT/requirements-qc.txt" "$ISO_ROOT/workspace/requirements-qc.txt"
  cp "$SOURCE_ROOT/requirements.txt" "$ISO_ROOT/workspace/requirements.txt"
else
# Avoid copying the host checkout's venv and transient Python caches.
tar \
  --exclude='./.sam-isolated' \
  --exclude='./.venv' \
  --exclude='./__pycache__' \
  --exclude='*/__pycache__' \
  -cf - -C "$SOURCE_ROOT" . | tar -xf - -C "$ISO_ROOT/workspace"
fi

if [[ "$INSTALL" == 1 ]]; then
  echo "Installing isolated requirements; caller must have explicitly enabled network." >&2
  python3 -m venv "$ISO_ROOT/venv"
  REQUIREMENTS="$ISO_ROOT/workspace/requirements.txt"
  "$ISO_ROOT/venv/bin/python" -m pip install -r "$REQUIREMENTS"
else
  echo "Prepared $ISO_ROOT/workspace"
  echo "Next (network-authorized channel only):"
  echo "  python3 -m venv '$ISO_ROOT/venv'"
  echo "  '$ISO_ROOT/venv/bin/python' -m pip install -r '$ISO_ROOT/workspace/requirements.txt'"
fi

printf 'source_commit='; git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null || echo unknown
printf 'isolated_root=%s\n' "$ISO_ROOT"
