#!/usr/bin/env bash
# Run from a copy of the project on the Raspberry Pi.
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "$0")")/setup_pi.py" "$@"
