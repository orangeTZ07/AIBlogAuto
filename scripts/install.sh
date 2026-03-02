#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"

printf "\n==> Creating virtual environment: %s\n" "$VENV_DIR"
python3 -m venv "$VENV_DIR"

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if ! python -m pip --version >/dev/null 2>&1; then
  printf "==> Bootstrapping pip with ensurepip\n"
  python -m ensurepip --upgrade
fi

printf "==> Upgrading pip/setuptools/wheel\n"
python -m pip install --upgrade pip setuptools wheel

printf "==> Installing BlogAuto\n"
pip install --no-build-isolation -e "$ROOT_DIR"

cat <<'EOF'

Install complete.

Run BlogAuto TUI:
  source .venv/bin/activate
  aiblogauto

DeepSeek key:
  export DEEPSEEK_API_KEY="your_key_here"

Nerd Font recommendation:
  install "JetBrainsMono Nerd Font" and set terminal font to it for the best icon rendering.
  https://www.nerdfonts.com/font-downloads
EOF
