#!/usr/bin/env bash
set -euo pipefail
mkdir -p /run/sshd /run/benchmark-ssh /root/.ssh
if [[ -f /input-keys/id_ed25519 ]]; then
  install -m 600 /input-keys/id_ed25519 /run/benchmark-ssh/id_ed25519
  install -m 600 /input-keys/id_ed25519 /root/.ssh/id_ed25519
  install -m 644 /input-keys/id_ed25519.pub /run/benchmark-ssh/id_ed25519.pub
  install -m 600 /input-keys/id_ed25519.pub /root/.ssh/authorized_keys
fi
chmod 700 /root/.ssh
printf '%s\n' 'Host party*' '  StrictHostKeyChecking accept-new' \
  '  UserKnownHostsFile /run/benchmark-ssh/known_hosts' > /root/.ssh/config
chmod 600 /root/.ssh/config
ssh-keygen -A
printf '%s\n' 'PermitRootLogin prohibit-password' 'PasswordAuthentication no' \
  'PubkeyAuthentication yes' > /etc/ssh/sshd_config.d/benchmark.conf
exec "$@"
