#!/usr/bin/env bash
# Push the current project to an already configured Pi. No Git remote required.
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# != 1 || $1 == --help || $1 == -h ]]; then
  echo "Usage: bash deploy/update-pi.sh USER@PI_HOST"
  echo "Example: bash deploy/update-pi.sh pi@192.168.1.50"
  echo "Run on your development computer. SSH and sudo may request passwords."
  [[ $# == 1 && ($1 == --help || $1 == -h) ]] && exit 0
  exit 2
fi
target=$1
# Keep the remote target and generated shell paths free of shell metacharacters.
if [[ ! $target =~ ^[a-zA-Z_][a-zA-Z0-9_-]*@[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]]; then
  echo "Expected USER@HOST (hostname or IPv4 address)." >&2
  exit 2
fi
for tool in python3 ssh scp; do
  command -v "$tool" >/dev/null || { echo "Missing command: $tool" >&2; exit 1; }
done
scratch=$(mktemp -d /tmp/desk-push.XXXXXXXX)
remote_dir=
ssh_options=(-o ControlMaster=auto -o ControlPersist=60 -o "ControlPath=$scratch/ssh" -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
cleanup() {
  result=$?
  trap - EXIT
  if [[ -n $remote_dir ]]; then
    ssh "${ssh_options[@]}" -o BatchMode=yes "$target" "rm -rf -- '$remote_dir'" </dev/null || true
  fi
  ssh "${ssh_options[@]}" -O exit "$target" >/dev/null 2>&1 || true
  rm -rf -- "$scratch"
  exit "$result"
}
trap cleanup EXIT
python3 "$source_dir/deploy/update_pi.py" --bundle "$scratch/project.tar.gz"
remote_dir=$(ssh "${ssh_options[@]}" "$target" 'umask 077; mktemp -d /tmp/desk-update.XXXXXXXX')
if [[ ! $remote_dir =~ ^/tmp/desk-update\.[a-zA-Z0-9]{8}$ ]]; then
  echo "Unexpected remote staging path; aborting." >&2
  remote_dir=
  exit 1
fi
scp "${ssh_options[@]}" "$scratch/project.tar.gz" "$source_dir/deploy/update_pi.py" "$target:$remote_dir/"
ssh -t "${ssh_options[@]}" "$target" "sudo python3 '$remote_dir/update_pi.py' --archive '$remote_dir/project.tar.gz'"
echo "Update finished. Tunnel: ssh -L 8081:127.0.0.1:8080 $target"
echo "Open http://127.0.0.1:8081/console"
