# grd-privacy

> **Note:** Most of this project was written by an AI (Claude, by Anthropic).

Keeps the local screen private while someone is connected via GNOME Remote
Desktop (desktop sharing on Wayland): the local monitors go black and local
keyboards, mice and touchpads are locked. The remote user keeps seeing and
controlling the desktop as usual.

## How it works

Two processes with separate privileges:

- **Watcher** (`grd-privacy.service`, user service): runs as your desktop
  user, *without* membership in the `input` group. Every 2 seconds it checks
  the session D-Bus for active Mutter RemoteDesktop sessions. While one
  exists, it sets the gamma ramps of all active CRTCs to zero and starts the
  input lock. Gamma is applied at scanout, so the remote stream is not
  affected.
- **Input lock** (`grd-input-lock@<PID>.service`, system service): runs as a
  hardened `DynamicUser` with group `input` and grabs local input devices via
  `EVIOCGRAB`. It releases them as soon as the watcher process `<PID>` is gone
  (fail-open), so a crashed watcher never leaves you locked out.

A polkit rule allows your user to start and stop `grd-input-lock@*.service`
instances without a password, and nothing else. Remote input goes through
Mutter directly and is not affected by the lock. Power/sleep buttons, the lid
switch and the video bus are never grabbed.

## Requirements

- GNOME on Wayland with GNOME Remote Desktop
- Linux 5.3 or newer
- Python 3.9 or newer with PyGObject and python-evdev
  - Fedora: `sudo dnf install python3-gobject python3-evdev`
  - Debian/Ubuntu: `sudo apt install python3-gi python3-evdev`
- systemd and polkit

## Installation

Run as your desktop user via sudo:

```sh
sudo ./install.sh
systemctl --user daemon-reload
systemctl --user enable --now grd-privacy
```

`install.sh` installs the script root-owned to `/usr/local/bin`, since it is
executed with group `input` and must not be writable by your user. If your
user is a member of `input`, remove it (`sudo gpasswd -d $USER input`, then
log in again); otherwise the privilege separation is pointless.

## Usage

The service runs on its own once enabled. For manual use:

```sh
grd-privacy.py --test 15   # blank and lock for 15 seconds, then release
grd-privacy.py --status    # list detected remote sessions
grd-privacy.py --restore   # reset monitors stuck at black
```

Further options for the watcher: `--interval SEC`, `--no-blank`, and
`--input systemd|local|off` (`local` locks inside the watcher process and
needs group `input`).

Logs:

```sh
journalctl --user -u grd-privacy
journalctl -u 'grd-input-lock@*'
```

## Troubleshooting

- **Screen stays black after a session:** run `grd-privacy.py --restore`
  (from an SSH or remote session if needed). The watcher also does this on
  every start.
- **Input not locked:** check the logs of `grd-input-lock@*` - usually
  python-evdev is missing or the polkit rule was not installed.

## Limitations

- Any Mutter RemoteDesktop session triggers the lock, including ones opened
  through the xdg-desktop-portal by other applications.
- Night Light or similar may briefly restore the gamma before the next check
  blanks it again.

## Uninstall

```sh
systemctl --user disable --now grd-privacy
rm ~/.config/systemd/user/grd-privacy.service
sudo rm /usr/local/bin/grd-privacy.py \
        /etc/systemd/system/grd-input-lock@.service \
        /etc/polkit-1/rules.d/50-grd-input-lock.rules
sudo systemctl daemon-reload
```

## License

MIT, see [LICENSE](LICENSE).
