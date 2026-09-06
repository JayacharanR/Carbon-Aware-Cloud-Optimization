#!/usr/bin/env bash

# Read-only by default.  This script is the Arch Linux companion to the
# Windows PowerShell checker.  It does not create Azure resources, configure a
# tunnel, or write .env values.  Use --install-arch-tools only after reviewing
# the pacman command and --install-uv only when the official uv installer is
# acceptable for the machine.

set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ARCH_TOOLS=0
INSTALL_UV=0
SYNC=0

usage() {
  cat <<'EOF'
Usage: bash scripts/install-prerequisites-arch.sh [options]

Options:
  --install-arch-tools  Install common packages with pacman (uses sudo).
  --install-uv           Install uv with Astral's official installer.
  --sync                 Run `uv sync --all-extras` after checks.
  --all                  Install the common Arch packages, uv, and sync.
  -h, --help             Show this help.

The default action only checks the local machine. Azure CLI, Ollama, and
Tailscale are intentionally not installed by the pacman list because their
availability/source varies on Arch; use each project's official Linux
instructions and then rerun this checker.
EOF
}

for argument in "$@"; do
  case "$argument" in
    --install-arch-tools) INSTALL_ARCH_TOOLS=1 ;;
    --install-uv) INSTALL_UV=1 ;;
    --sync) SYNC=1 ;;
    --all)
      INSTALL_ARCH_TOOLS=1
      INSTALL_UV=1
      SYNC=1
      ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n\n' "$argument" >&2; usage >&2; exit 2 ;;
  esac
done

if (( INSTALL_ARCH_TOOLS )); then
  if ! command -v pacman >/dev/null 2>&1; then
    printf 'pacman was not found; this helper is for Arch Linux.\n' >&2
    exit 1
  fi
  sudo pacman -Syu --needed \
    base-devel \
    ca-certificates \
    curl \
    docker \
    docker-compose \
    git \
    helm \
    jq \
    kubectl \
    python \
    python-pip \
    unzip
fi

if (( INSTALL_UV )) && ! command -v uv >/dev/null 2>&1; then
  curl --fail --location --silent --show-error https://astral.sh/uv/install.sh | sh
fi

# The installer normally places uv here.  Exporting it for this process makes
# `--install-uv --sync` work without requiring a new shell; it does not alter
# the caller's shell startup files.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

python_command=''
if command -v python3 >/dev/null 2>&1; then
  python_command="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  python_command="$(command -v python)"
fi

failures=()
if [[ -z "$python_command" ]]; then
  failures+=("python 3.11-3.13")
else
  python_version="$($python_command -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  if ! "$python_command" -c 'import sys; raise SystemExit(not (sys.version_info >= (3, 11) and sys.version_info < (3, 14)))'; then
    failures+=("Python 3.11-3.13 (found $python_version)")
  else
    printf 'OK  python: %s (%s)\n' "$python_command" "$python_version"
  fi
fi

for command_name in git curl; do
  if command -v "$command_name" >/dev/null 2>&1; then
    printf 'OK  %s: %s\n' "$command_name" "$(command -v "$command_name")"
  else
    failures+=("$command_name")
    printf 'MISSING: %s\n' "$command_name" >&2
  fi
done

if command -v uv >/dev/null 2>&1; then
  printf 'OK  uv: %s\n' "$(uv --version)"
else
  failures+=("uv (install with --install-uv or the documented command)")
  printf 'MISSING: uv (install with --install-uv)\n' >&2
fi

optional_commands=(az kubectl helm docker ollama tailscale)
for command_name in "${optional_commands[@]}"; do
  if command -v "$command_name" >/dev/null 2>&1; then
    printf 'OK  %s: %s\n' "$command_name" "$(command -v "$command_name")"
  else
    printf 'WARN %s is not installed (needed only for the corresponding cloud/LLM/tunnel path)\n' "$command_name"
  fi
done

if (( ${#failures[@]} > 0 )); then
  printf '\nMissing or invalid prerequisites:\n' >&2
  printf '  - %s\n' "${failures[@]}" >&2
  exit 1
fi

if (( SYNC )); then
  cd "$REPO_ROOT"
  uv sync --all-extras
fi

cat <<'EOF'

Prerequisite check completed.  The project Python environment is managed by
uv; use `uv run ...` for Python commands.  If Docker was installed, start it
only when needed with `sudo systemctl enable --now docker` and configure the
operator's docker group membership separately.

For the real Azure path, install Azure CLI, Ollama, and Tailscale using their
official Linux instructions for your Arch setup.  This repository does not
invent those package sources or credentials.
EOF
