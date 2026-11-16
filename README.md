# A1X Controller (Decky plugin)

Manage the AOKZOE A1X integrated controller on Bazzite (InputPlumber) from the Quick Access Menu.

- **Enabled toggle**: turning it off stops the integrated controller's InputPlumber composite device, so games and Steam see nothing from it. OpenGamepadUI's launch manager can't bring it back, since there is no device left to reconfigure. Turning it on restarts InputPlumber, which recreates the device.
- **Failsafe** (on by default): while the controller is disabled, if no other physical gamepad is visible for 10 seconds, the plugin re-enables the integrated controller so you are never locked out.
- **Restart Controller**: restarts `inputplumber.service`.
- **Reset Pad**: shown when another process (for example Steam) has the raw pad open, which is what causes the duplicate "Xbox 360 Controller". It unbinds and rebinds the pad's USB interface so the stale handle is dropped.
- **Diagnostics**: InputPlumber composite devices, the raw pad's nodes and who holds them, and the physical gamepads that games can see.

## Why the duplicate controller happens

InputPlumber hides the raw pad (`045e:028e`, driven by xpad) only *after* its device node appears, and its hide rules are tied to the input number, which changes every time the pad re-enumerates (eGPU dock, resume, rebind) or InputPlumber restarts. In that gap of about 150 ms, Steam opens the raw pad and keeps it, so it appears twice.

The plugin installs `udev/72-a1x-hide-integrated-pad.rules` to `/etc/udev/rules.d/`. It matches the pad by vendor, product and serial, and keeps it hidden from the moment it is created. It has to sort after `71-microsoft-gdu.rules`, which grants `uaccess` to Xbox 360 pads, and before `73-seat-late.rules`, which turns that into a user ACL. InputPlumber runs as root and still reads the pad. The rule is removed when the plugin is uninstalled.

Trade-off: while the rule is installed, the built-in pad gives no input at all if InputPlumber isn't running. Use Restart Controller to recover.

## Build

```sh
scripts/package.sh   # creates out/a1x-integrated-controller.zip
```

Uses a local `pnpm` if one is installed, otherwise a `node:22-alpine` container via podman or docker.

## Install on the device

Copy the zip to the device, then in Decky go to Settings → Developer → Install Plugin from ZIP File.

## Debugging

`scripts/diagnose.sh` prints InputPlumber devices, kernel input and hidraw devices, and which nodes Steam has open. Run it on the device before and after a change and diff the output.

Logs: `~/homebrew/logs/a1x-integrated-controller/`.
