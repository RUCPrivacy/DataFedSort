#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
directory="$root/workspace/local_cluster"
mkdir -p "$directory"
if [[ -e "$directory/id_ed25519" || -e "$directory/id_ed25519.pub" ]]; then
  [[ -f "$directory/id_ed25519" && -f "$directory/id_ed25519.pub" ]] || {
    echo 'Incomplete local cluster key pair; inspect it before replacing anything.' >&2
    exit 1
  }
  echo 'Using the existing user-generated local cluster key pair.'
else
  ssh-keygen -q -t ed25519 -N '' -C datafedsort-local-cluster -f "$directory/id_ed25519"
fi
echo 'Keys stay in ignored workspace/local_cluster; never include them in an artifact.'
