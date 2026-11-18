# USB access on macOS

The browser server and Python process run as your ordinary account. Only the
native capture helper communicates with the camera's USB interfaces.

If the OS refuses USB access, authorize the capture helper in your terminal:

```sh
sudo -v
femto-bolt-viewer run \
  --serial YOUR_CAMERA_SERIAL \
  --nvram /path/to/your-camera/nvram \
  --native-profile-dir /path/to/your-camera/native-profiles \
  --capture-bin build/native/bin/femto_live_capture \
  --mode wfov-binned \
  --sudo
```

`sudo -v` asks for your administrator password in Terminal. The viewer's `--sudo`
option invokes only the native capture binary through `sudo -n`; it cannot prompt
for a password. If authorization is missing or expires before the capture starts,
run `sudo -v` again in the terminal where you launch the viewer. Inspect and trust
the native executable you authorize before launching it.

Do not run Python or the whole web server under `sudo`. This repository does not
install a passwordless helper or alter your system authorization configuration.

If another application is already using the Bolt, close its camera session before
starting this viewer. Use a USB 3 data cable and the camera's normal power supply.
An authorization error and an unavailable or busy camera are separate problems;
inspect the native capture error before changing permissions.
