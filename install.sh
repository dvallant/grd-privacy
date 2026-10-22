#!/bin/bash
# Installs grd-privacy. Run as your desktop user: sudo ./install.sh
set -euo pipefail
U="${SUDO_USER:?Please run as your desktop user via sudo: sudo ./install.sh}"
G="$(id -gn "$U")"
H="$(getent passwd "$U" | cut -d: -f6)"
cd "$(dirname "$0")"

# Install the script root-owned: the lock service runs it with group "input",
# so your user must not be able to modify it.
install -o root -g root -m 0755 grd-privacy.py /usr/local/bin/grd-privacy.py
install -o root -g root -m 0644 'grd-input-lock@.service' /etc/systemd/system/
sed "s/__USER__/$U/g" 50-grd-input-lock.rules > /etc/polkit-1/rules.d/50-grd-input-lock.rules
chmod 0644 /etc/polkit-1/rules.d/50-grd-input-lock.rules
systemctl daemon-reload

sudo -u "$U" mkdir -p "$H/.config/systemd/user"
install -o "$U" -g "$G" -m 0644 grd-privacy.service "$H/.config/systemd/user/grd-privacy.service"

echo "Done. Now run as $U (without sudo):"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now grd-privacy"
if id -nG "$U" | grep -qw input; then
    echo
    echo "Note: $U is still a member of group 'input'. Remove with:"
    echo "  sudo gpasswd -d $U input   (then log in again)"
fi
