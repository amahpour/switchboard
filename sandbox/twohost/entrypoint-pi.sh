#!/bin/bash
# The fake remote machine (docs/SANDBOX.md §11, docs/DEMO-FPGA.md §7): runs as the
# unprivileged user `pi`, with no capabilities. It starts
#   - a user-level sshd on port 2222 (its own host key under ~/.sshd, its own config,
#     AuthorizedKeysFile ~/.ssh/authorized_keys, no PAM, no passwords), which is what
#     the desktop's link dials and what `switchboard remote accept` writes its line for;
#   - the fake board (a pty "UART" at ~/bench/ttyFAKE0);
# and puts the bench scripts in ~/bench. It prints the sshd host key first, so the
# desktop can compare it with what it pins (`remote add`). No system sshd, no root.
set -euo pipefail
umask 077
cd "$HOME"
mkdir -p .sshd .ssh bench fpga/in
chmod 700 .sshd .ssh bench
if [ ! -f .sshd/host_ed25519 ]; then
  ssh-keygen -q -t ed25519 -N '' -C fake-remote-host -f .sshd/host_ed25519
fi
[ -f .ssh/authorized_keys ] || : > .ssh/authorized_keys
chmod 600 .ssh/authorized_keys
install -m 0700 /opt/bench/uart_test.py bench/uart_test.py
cat > .sshd/sshd_config <<EOF
Port 2222
ListenAddress 0.0.0.0
HostKey $HOME/.sshd/host_ed25519
PidFile $HOME/.sshd/sshd.pid
AuthorizedKeysFile $HOME/.ssh/authorized_keys
AllowUsers $(id -un)
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
X11Forwarding no
# the link and the push key need none of these; a hand-added key line without
# restrict gets no forwarding, tunnel or ~/.ssh/rc either
DisableForwarding yes
AllowAgentForwarding no
AllowTcpForwarding no
AllowStreamLocalForwarding no
PermitTunnel no
PermitUserRC no
PermitUserEnvironment no
PrintMotd no
LogLevel INFO
EOF
/usr/sbin/sshd -t -f .sshd/sshd_config
echo "fake remote: sshd host key $(ssh-keygen -l -f .sshd/host_ed25519.pub)"
echo "fake remote: host key line: $(cut -d' ' -f1,2 .sshd/host_ed25519.pub)"
/usr/bin/python3 /opt/bench/fake_board.py --dir "$HOME/bench" &
exec /usr/sbin/sshd -D -e -f "$HOME/.sshd/sshd_config"
