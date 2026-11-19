"""
A1X Controller - Decky backend.

Talks to InputPlumber over the system DBus (via `busctl`, so no Python DBus
bindings are required in Decky's bundled interpreter).

Disable: Stop() the integrated controller's InputPlumber composite device.
         With no composite device there are no virtual targets, and nothing
         (e.g. OpenGamepadUI's LaunchManager, which resets target devices on
         every game launch) can bring them back.
Enable:  restart inputplumber.service, which recreates the composite device.
Restart: same restart, exposed explicitly.

The raw pad stays hidden throughout thanks to a udev rule this plugin installs
(udev/72-a1x-hide-integrated-pad.rules). Without it, InputPlumber only hides
the pad *after* its device node appears, and Steam opens the raw pad in that
gap (USB re-enumeration on eGPU dock/resume, InputPlumber restart), which shows
up as a duplicate "Xbox 360 Controller" in Steam.
"""

import asyncio
import glob
import json
import os
import re
import shutil

import decky

BUS = "org.shadowblip.InputPlumber"
COMPOSITE_IFACE = "org.shadowblip.Input.CompositeDevice"
MANAGER_PATH = "/org/shadowblip/InputPlumber/Manager"
MANAGER_IFACE = "org.shadowblip.InputManager"
SERVICE = "inputplumber.service"
COMPOSITE_RE = re.compile(r"/CompositeDevice\d+$")

# The integrated pad: xpad-driven USB device 045e:028e with this serial.
PAD_VENDOR, PAD_PRODUCT, PAD_SERIAL = "045e", "028e", "0123456789"

RULE_NAME = "72-a1x-hide-integrated-pad.rules"
RULE_SRC = os.path.join(decky.DECKY_PLUGIN_DIR, "udev", RULE_NAME)
RULE_DST = os.path.join("/etc/udev/rules.d", RULE_NAME)

SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
DEFAULT_SETTINGS = {
    # Last name we saw for it, so we still know what to look for while stopped.
    "last_seen_name": None,
    # Re-enable automatically if no other gamepad is present while disabled.
    "failsafe": True,
}
FAILSAFE_GRACE_SECONDS = 10
WATCH_INTERVAL_SECONDS = 2


def _clean_env():
    # Decky's PyInstaller runtime sets LD_LIBRARY_PATH, which breaks system binaries.
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(key, None)
    return env


async def _run(*args, timeout=15):
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_clean_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Timed out: {' '.join(args)}")
    if proc.returncode != 0:
        raise RuntimeError(err.decode().strip() or f"{args[0]} exited {proc.returncode}")
    return out.decode()


async def _get_prop(path, iface, prop):
    out = await _run("busctl", "--system", "--json=short", "get-property", BUS, path, iface, prop)
    return json.loads(out).get("data")


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _list_gamepads():
    """Physical joysticks visible to games.

    Skips nodes InputPlumber has hidden (e.g. the integrated pad itself) and
    uinput virtual pads (Steam's and InputPlumber's), which exist only as
    mirrors of other controllers. Bluetooth pads live under
    /devices/virtual/misc/uhid, so they are still counted.
    """
    pads = []
    for js in sorted(glob.glob("/sys/class/input/js*")):
        node = os.path.basename(js)
        if os.path.exists(os.path.join("/dev/inputplumber/by-hidden", node)):
            continue
        if "/devices/virtual/input/" in os.path.realpath(js):
            continue
        if _is_pad_node(node):
            continue
        pads.append({"node": node, "name": _read(os.path.join(js, "device", "name")) or "?"})
    return pads


def _pad_usb_device(sys_input_node):
    """Return the USB device dir if this /sys/class/input/<node> is the integrated pad."""
    path = os.path.realpath(sys_input_node)
    while path and path != "/":
        if os.path.exists(os.path.join(path, "idVendor")):
            if (
                _read(os.path.join(path, "idVendor")) == PAD_VENDOR
                and _read(os.path.join(path, "idProduct")) == PAD_PRODUCT
                and _read(os.path.join(path, "serial")) == PAD_SERIAL
            ):
                return path
            return None
        path = os.path.dirname(path)
    return None


def _is_pad_node(node):
    return _pad_usb_device(os.path.join("/sys/class/input", node)) is not None


def _pad_nodes():
    """Device node names (eventN, jsN) belonging to the integrated pad."""
    nodes = []
    for p in glob.glob("/sys/class/input/event*") + glob.glob("/sys/class/input/js*"):
        if _pad_usb_device(p):
            nodes.append(os.path.basename(p))
    return sorted(nodes)


def _pad_interface():
    """(usb interface name, driver dir) for the integrated pad, e.g. ('3-3:1.0', .../xpad)."""
    for p in glob.glob("/sys/class/input/event*"):
        if _pad_usb_device(p):
            # .../3-3/3-3:1.0/input/inputN/eventN -> interface is two levels up from inputN
            iface = os.path.realpath(os.path.join(p, "device", "device"))
            driver = os.path.realpath(os.path.join(iface, "driver"))
            if os.path.isdir(driver):
                return os.path.basename(iface), driver
    return None, None


def _raw_pad_holders():
    """Processes (other than InputPlumber) holding the raw pad's device nodes open."""
    targets = {os.path.join("/dev/input", n) for n in _pad_nodes()}
    if not targets:
        return []
    holders = []
    for proc in glob.glob("/proc/[0-9]*"):
        comm = _read(os.path.join(proc, "comm")) or "?"
        if comm.startswith("inputplumber"):
            continue
        try:
            fds = os.listdir(os.path.join(proc, "fd"))
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(os.path.join(proc, "fd", fd))
            except OSError:
                continue
            if link in targets:
                holders.append({"pid": int(os.path.basename(proc)), "comm": comm, "node": link})
    return holders


class Plugin:
    settings = dict(DEFAULT_SETTINGS)
    watch_task = None
    last_error = None

    # ---------- settings ----------
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE) as f:
                self.settings = {**DEFAULT_SETTINGS, **json.load(f)}
        except (OSError, ValueError):
            self.settings = dict(DEFAULT_SETTINGS)

    def _save_settings(self):
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(self.settings, f, indent=2)

    # ---------- udev rule ----------
    def _rule_installed(self):
        return _read(RULE_DST) is not None and _read(RULE_DST) == _read(RULE_SRC)

    async def _ensure_rule(self):
        """Install/refresh the hide rule. Returns True if it was (re)written."""
        if self._rule_installed():
            return False
        shutil.copyfile(RULE_SRC, RULE_DST)
        os.chmod(RULE_DST, 0o644)
        await _run("udevadm", "control", "--reload")
        decky.logger.info(f"Installed {RULE_DST}")
        return True

    async def _rebind_pad(self):
        """Re-enumerate the pad so the hide rule applies and stale raw handles die."""
        iface, driver = _pad_interface()
        if not iface:
            raise RuntimeError("Integrated pad not found in sysfs")
        with open(os.path.join(driver, "unbind"), "w") as f:
            f.write(iface)
        await asyncio.sleep(1)
        with open(os.path.join(driver, "bind"), "w") as f:
            f.write(iface)
        decky.logger.info(f"Rebound {iface} on {os.path.basename(driver)}")

    # ---------- InputPlumber queries ----------
    async def _composites(self):
        out = await _run("busctl", "--system", "tree", "--list", BUS)
        paths = sorted(
            (p.strip() for p in out.splitlines() if COMPOSITE_RE.search(p.strip())),
            key=lambda p: int(re.search(r"(\d+)$", p).group(1)),
        )
        devices = []
        for path in paths:
            dev = {"path": path}
            for prop, key in (
                ("Name", "name"),
                ("ProfileName", "profile"),
                ("SourceDevicePaths", "sources"),
                ("TargetDevices", "targets"),
            ):
                try:
                    dev[key] = await _get_prop(path, COMPOSITE_IFACE, prop)
                except Exception:
                    dev[key] = None
            devices.append(dev)
        return devices

    def _pick_name(self, composites):
        names = [c["name"] for c in composites if c.get("name")]
        for n in names:
            if re.search(r"aokzoe|a1\s?x|onexplayer", n, re.I):
                return n
        # While stopped, the integrated device is absent: fall back to the name we
        # last saw rather than picking some other (e.g. Bluetooth) composite device.
        if self.settings.get("last_seen_name"):
            return self.settings["last_seen_name"]
        return names[0] if names else None

    def _remember(self, name, composites):
        if name and any(c.get("name") == name for c in composites) and self.settings.get("last_seen_name") != name:
            self.settings["last_seen_name"] = name
            self._save_settings()

    async def _state(self):
        """(inputplumber_running, composites, selected_name, disabled)."""
        try:
            composites = await self._composites()
        except Exception:
            return False, [], self._pick_name([]), False
        name = self._pick_name(composites)
        self._remember(name, composites)
        disabled = bool(name) and not any(c.get("name") == name for c in composites)
        return True, composites, name, disabled

    # ---------- callables ----------
    async def get_status(self):
        status = {
            "available": False,
            "error": None,
            "version": None,
            "composites": [],
            "selected": None,
            "disabled": False,
            "duplicates": [],
            "gamepads": _list_gamepads(),
            "pad_nodes": _pad_nodes(),
            "raw_holders": _raw_pad_holders(),
            "rule_installed": self._rule_installed(),
            "failsafe": self.settings.get("failsafe", True),
            "last_error": self.last_error,
        }
        try:
            status["version"] = await _get_prop(MANAGER_PATH, MANAGER_IFACE, "Version")
            running, composites, name, disabled = await self._state()
            status["available"] = running
            status["composites"] = composites
            status["selected"] = name
            status["disabled"] = disabled
            seen = {}
            for c in composites:
                seen[c.get("name")] = seen.get(c.get("name"), 0) + 1
            status["duplicates"] = [n for n, count in seen.items() if n and count > 1]
        except Exception as e:
            status["error"] = str(e)
        return status

    async def set_failsafe(self, enabled):
        self.settings["failsafe"] = bool(enabled)
        self._save_settings()
        return True

    async def disable_controller(self):
        self.last_error = None
        try:
            # Stopping the composite device makes InputPlumber drop its own hide
            # rules; ours must be in place so the raw pad stays hidden.
            await self._ensure_rule()
            composites = await self._composites()
            name = self._pick_name(composites)
            paths = [c["path"] for c in composites if c.get("name") == name]
            if not paths:
                raise RuntimeError(f"No InputPlumber device named '{name}' found")
            self._remember(name, composites)
            for path in paths:
                await _run("busctl", "--system", "call", BUS, path, COMPOSITE_IFACE, "Stop")
            decky.logger.info(f"Stopped integrated controller '{name}' ({paths})")
            return {"ok": True}
        except Exception as e:
            self.last_error = str(e)
            decky.logger.error(f"disable failed: {e}")
            return {"ok": False, "error": str(e)}

    async def restart_inputplumber(self):
        self.last_error = None
        try:
            await _run("systemctl", "restart", SERVICE, timeout=30)
            # Give InputPlumber a moment to re-create its composite devices.
            for _ in range(20):
                await asyncio.sleep(0.5)
                try:
                    if await self._composites():
                        break
                except Exception:
                    pass
            decky.logger.info("Restarted InputPlumber")
            return {"ok": True}
        except Exception as e:
            self.last_error = str(e)
            decky.logger.error(f"restart failed: {e}")
            return {"ok": False, "error": str(e)}

    async def enable_controller(self):
        return await self.restart_inputplumber()

    async def reset_pad(self):
        """Re-enumerate the pad's USB interface to drop stale raw handles (e.g. Steam's)."""
        self.last_error = None
        try:
            await self._ensure_rule()
            await self._rebind_pad()
            await asyncio.sleep(1)
            return {"ok": True}
        except Exception as e:
            self.last_error = str(e)
            decky.logger.error(f"reset failed: {e}")
            return {"ok": False, "error": str(e)}

    # ---------- failsafe watcher ----------
    async def _watch(self):
        no_pad_since = None
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.sleep(WATCH_INTERVAL_SECONDS)
                if not self.settings.get("failsafe", True):
                    no_pad_since = None
                    continue
                running, _, _, disabled = await self._state()
                if not running or not disabled or _list_gamepads():
                    no_pad_since = None
                    continue
                now = loop.time()
                no_pad_since = no_pad_since or now
                if now - no_pad_since >= FAILSAFE_GRACE_SECONDS:
                    decky.logger.warning("No other gamepad present; failsafe re-enabling integrated controller")
                    await self.enable_controller()
                    await decky.emit("failsafe_triggered")
                    no_pad_since = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"watcher error: {e}")

    # ---------- lifecycle ----------
    async def _main(self):
        self._load_settings()
        try:
            if await self._ensure_rule() and _raw_pad_holders():
                # First install: something (Steam) already has the raw pad open.
                await self._rebind_pad()
        except Exception as e:
            self.last_error = f"udev rule setup failed: {e}"
            decky.logger.error(self.last_error)
        self.watch_task = asyncio.get_running_loop().create_task(self._watch())
        decky.logger.info("A1X Controller loaded")

    async def _unload(self):
        if self.watch_task:
            self.watch_task.cancel()
        # Never leave the user without their built-in controller.
        try:
            running, _, _, disabled = await self._state()
            if running and disabled:
                await self.enable_controller()
        except Exception as e:
            decky.logger.error(f"unload re-enable failed: {e}")
        decky.logger.info("A1X Controller unloaded")

    async def _uninstall(self):
        try:
            if os.path.exists(RULE_DST):
                os.remove(RULE_DST)
                await _run("udevadm", "control", "--reload")
                decky.logger.info(f"Removed {RULE_DST}")
        except Exception as e:
            decky.logger.error(f"uninstall cleanup failed: {e}")
