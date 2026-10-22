#!/usr/bin/env python3
"""
grd-privacy - protects the local screen during a GNOME Remote Desktop
session (desktop sharing on Wayland).

Components:
  Watcher   (user service, your user, WITHOUT group "input"):
            detects Mutter RemoteDesktop sessions, blanks the screen via
            gamma and starts/stops the input lock via systemctl.
  Lock      (system service grd-input-lock@<PID>.service, DynamicUser
            with group "input"): grabs local keyboards, mice and
            touchpads and releases them as soon as the watcher process
            <PID> no longer exists (fail-open).

Remote input goes through Mutter internally and is not affected.

Usage:
  grd-privacy.py              watcher (service mode)
  grd-privacy.py --test 15    lock for 15 seconds, then release
  grd-privacy.py --status     show detected remote sessions
  grd-privacy.py --restore    reset monitors stuck at black
  grd-privacy.py --lock-only --watch-pid PID
                              input lock only (started by the system
                              service, not meant for manual use)
"""

import argparse
import glob
import os
import signal
import subprocess
import sys
import time

try:
    import evdev
    from evdev import ecodes as e
except ImportError:
    evdev = None

# Devices whose name contains one of these substrings are never locked
EXCLUDE_NAMES = ["Power Button", "Sleep Button", "Lid Switch", "Video Bus"]

LOCK_UNIT = "grd-input-lock"

RD_BUS = "org.gnome.Mutter.RemoteDesktop"
RD_SESSIONS = "/org/gnome/Mutter/RemoteDesktop/Session"
DC = ("org.gnome.Mutter.DisplayConfig",
      "/org/gnome/Mutter/DisplayConfig",
      "org.gnome.Mutter.DisplayConfig")


def log(msg):
    print(msg, flush=True)


def linear(n):
    return [round(i * 65535 / (n - 1)) for i in range(n)] if n > 1 else [65535]


def is_black(ramps):
    return not any(any(r) for r in ramps)


# --------------------------------------------------------------------------
# Watcher part (session D-Bus)
# --------------------------------------------------------------------------

class Bus:
    def __init__(self):
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def call(self, name, path, iface, method, params=None):
        res = self.conn.call_sync(name, path, iface, method, params, None,
                                  self.Gio.DBusCallFlags.NONE, 5000, None)
        return res.unpack() if res is not None else ()

    def remote_sessions(self):
        """Paths of all active Mutter RemoteDesktop sessions."""
        try:
            (xml,) = self.call(RD_BUS, RD_SESSIONS,
                               "org.freedesktop.DBus.Introspectable",
                               "Introspect")
            info = self.Gio.DBusNodeInfo.new_for_xml(xml)
            return [n.path for n in info.nodes]
        except self.GLib.Error:
            return []


class Blanker:
    """Sets the gamma ramps of all active CRTCs to zero."""

    def __init__(self, bus):
        self.bus = bus
        self.V = bus.GLib.Variant
        self.saved = {}      # crtc_id -> (r, g, b)
        self.error = False

    def _crtcs(self):
        res = self.bus.call(*DC, "GetResources")
        serial, crtcs = res[0], res[1]
        return serial, [c[0] for c in crtcs if c[6] != -1]

    def _get(self, serial, crtc):
        return self.bus.call(*DC, "GetCrtcGamma",
                             self.V("(uu)", (serial, crtc)))

    def _set(self, serial, crtc, r, g, b):
        self.bus.call(*DC, "SetCrtcGamma",
                      self.V("(uuaqaqaq)", (serial, crtc, r, g, b)))

    def apply(self):
        """Blank; called periodically and overrides Night Light etc."""
        try:
            serial, crtcs = self._crtcs()
            for crtc in crtcs:
                cur = self._get(serial, crtc)
                self.saved.setdefault(crtc, cur)
                if not is_black(cur):
                    zero = [0] * len(cur[0])
                    self._set(serial, crtc, zero, zero, zero)
            self.error = False
        except self.bus.GLib.Error as err:
            if not self.error:
                log(f"Blanking failed: {err.message}")
            self.error = True

    def restore(self, only_black=False):
        try:
            serial, crtcs = self._crtcs()
            for crtc in crtcs:
                cur = self._get(serial, crtc)
                if only_black and not is_black(cur):
                    continue
                ramps = self.saved.get(crtc, cur)
                # Never "restore" a black ramp
                if is_black(ramps) or len(ramps[0]) != len(cur[0]):
                    ramps = (linear(len(cur[0])),) * 3
                self._set(serial, crtc, *ramps)
        except self.bus.GLib.Error as err:
            log(f"Restoring gamma failed: {err.message}")
        self.saved.clear()


class SystemdLock:
    """Starts/stops the input lock as a system service (via polkit rule)."""

    def __init__(self, unit=LOCK_UNIT):
        self.unit = f"{unit}@{os.getpid()}.service"
        self.active = False
        self.error = False

    def _ctl(self, verb):
        r = subprocess.run(["systemctl", "--no-ask-password", verb, self.unit],
                           capture_output=True, text=True)
        if r.returncode != 0:
            if not self.error:
                log(f"systemctl {verb} {self.unit} failed: "
                    f"{r.stderr.strip()}")
            self.error = True
            return False
        self.error = False
        return True

    def apply(self):
        if not self.active:
            self.active = self._ctl("start")
            if self.active:
                log(f"Input lock started ({self.unit})")

    def release(self):
        if self.active:
            self._ctl("stop")
            log("Input lock stopped")
        self.active = False


# --------------------------------------------------------------------------
# Lock part (/dev/input, runs in the system service or locally)
# --------------------------------------------------------------------------

class InputLock:
    """Grabs local keyboards, mice and touchpads via EVIOCGRAB."""

    def __init__(self):
        self.grabbed = {}    # path -> InputDevice
        self.skipped = set()

    @staticmethod
    def _is_target(dev):
        if any(x.lower() in dev.name.lower() for x in EXCLUDE_NAMES):
            return False
        caps = dev.capabilities(absinfo=False)
        keys = set(caps.get(e.EV_KEY, []))
        rel = set(caps.get(e.EV_REL, []))
        absa = set(caps.get(e.EV_ABS, []))
        keyboard = e.KEY_A in keys
        mouse = e.REL_X in rel and e.BTN_LEFT in keys
        touch = ((e.ABS_X in absa or e.ABS_MT_POSITION_X in absa)
                 and (e.BTN_TOUCH in keys or e.BTN_LEFT in keys))
        return keyboard or mouse or touch

    @staticmethod
    def _wait_released(dev, timeout=3.0):
        # Don't "freeze" pressed keys (would cause endless key repeat in Mutter)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if not dev.active_keys():
                    return
            except OSError:
                return
            time.sleep(0.05)

    def apply(self):
        """Lock; called periodically and picks up newly plugged-in devices."""
        paths = set(evdev.list_devices())
        for p in list(self.grabbed):
            if p not in paths:
                self._drop(p)
        self.skipped &= paths
        for p in paths - set(self.grabbed) - self.skipped:
            try:
                dev = evdev.InputDevice(p)
            except OSError:
                self.skipped.add(p)
                continue
            if not self._is_target(dev):
                dev.close()
                self.skipped.add(p)
                continue
            self._wait_released(dev)
            try:
                dev.grab()
            except OSError as err:
                log(f"Failed to grab {dev.name}: {err}")
                dev.close()
                self.skipped.add(p)
                continue
            self.grabbed[p] = dev
            log(f"Grabbed: {dev.name} ({p})")

    def _drop(self, p):
        dev = self.grabbed.pop(p)
        for fn in (dev.ungrab, dev.close):
            try:
                fn()
            except OSError:
                pass
        log(f"Released: {dev.name}")

    def release(self):
        for p in list(self.grabbed):
            self._drop(p)
        self.skipped.clear()


def input_available():
    if evdev is None:
        log("python3-evdev is missing - input lock not possible")
        return False
    if not any(os.access(p, os.R_OK) for p in glob.glob("/dev/input/event*")):
        log("No access to /dev/input/event* - input lock not possible")
        return False
    return True


def run_lock_only(pid, interval):
    """System service mode: lock as long as the watcher is alive."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
    except OSError:
        log(f"Watcher process {pid} does not exist")
        sys.exit(2)
    if b"grd-privacy" not in cmdline:
        log(f"Process {pid} is not a grd-privacy watcher")
        sys.exit(2)
    if not input_available():
        sys.exit(1)

    lock = InputLock()
    try:
        while os.path.exists(f"/proc/{pid}"):
            lock.apply()
            time.sleep(interval)
        log("Watcher process exited")
    finally:
        lock.release()


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=2.0,
                    help="check interval in seconds (default: 2)")
    ap.add_argument("--no-blank", action="store_true", help="don't blank the screen")
    ap.add_argument("--input", choices=["systemd", "local", "off"],
                    default="systemd",
                    help="input lock via system service (default), "
                         "in this process (needs group input) or off")
    ap.add_argument("--test", type=float, metavar="SEC",
                    help="lock immediately for SEC seconds, then release")
    ap.add_argument("--status", action="store_true",
                    help="show detected remote sessions")
    ap.add_argument("--restore", action="store_true",
                    help="reset monitors stuck at black")
    ap.add_argument("--lock-only", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--watch-pid", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(0))

    if args.lock_only:
        if args.watch_pid is None:
            ap.error("--lock-only requires --watch-pid")
        try:
            run_lock_only(args.watch_pid, args.interval)
        except KeyboardInterrupt:
            pass
        return

    bus = Bus()

    if args.status:
        sessions = bus.remote_sessions()
        log(f"Active remote sessions: {len(sessions)}")
        for s in sessions:
            log(f"  {RD_SESSIONS}/{s}")
        return
    if args.restore:
        Blanker(bus).restore(only_black=True)
        return

    blanker = None if args.no_blank else Blanker(bus)
    lock = None
    if args.input == "systemd":
        lock = SystemdLock()
    elif args.input == "local" and input_available():
        lock = InputLock()

    def engage():
        if blanker:
            blanker.apply()
        if lock:
            lock.apply()

    def release():
        if lock:
            lock.release()
        if blanker:
            blanker.restore()

    engaged = False
    try:
        if args.test:
            log(f"Test mode: locking in 1 s for {args.test:.0f} s")
            time.sleep(1)
            engaged = True
            end = time.monotonic() + args.test
            while time.monotonic() < end:
                engage()
                time.sleep(max(0.0, min(args.interval, end - time.monotonic())))
            return

        if blanker:
            blanker.restore(only_black=True)   # recover after a crash
        log("Waiting for remote desktop sessions ...")
        while True:
            if bus.remote_sessions():
                if not engaged:
                    log("Remote session detected - locking locally")
                    engaged = True
                engage()
            elif engaged:
                log("Remote session ended - releasing")
                release()
                engaged = False
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if engaged:
            release()
            log("Released, exiting.")


if __name__ == "__main__":
    main()
