# Codex Pet

A standalone dragon desktop companion, based on the supplied character
reference. She blinks, responds to your clicks with a greeting and a two-frame waving
animation, and can patrol across the desktop. Her patrol moves the whole illustrated
pose; it is not a frame-by-frame walking animation or a skeletal rig.

## Install or upgrade

Build with `./build-deb.sh`. Install the resulting package with:

```sh
sudo apt install ./dist/codex-pet_1.2.0_all.deb
```

If the previous pet is already open, right-click it and choose **退出** (Quit)
before starting the new version: upgrading the package does not replace a
running process. Open **Codex Pet** from the application menu, or run
`codex-pet` in a terminal.

## Interaction

- Click her to receive a greeting. Double-click for a different greeting.
- Drag with the left mouse button to move her.
- Right-click for the menu: greet, replay the arrival, turn desktop patrol on
  or off, or quit. On X11, patrol is enabled initially; she pauses between
  movements and turns around at the screen edges. It can be distracting, so
  it can be disabled in that menu.

This application is independent of the VS Code Codex extension: its arrival
animation plays when **the pet** launches. The desktop must support transparent
windows and compositing. Transparent areas of the window may still receive
clicks. Desktop window managers may ignore always-on-top hints.

On native Wayland, automatic patrol is disabled because ordinary applications
cannot set their window position; dragging requests a compositor-managed move,
whose availability depends on the compositor. X11/XWayland behavior can vary
with the window manager. The transparent window needs an X11 compositing window
manager on X11.

## Development

Run `/usr/bin/python3 src/codex_pet.py`. Tests:

```sh
/usr/bin/python3 -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen /usr/bin/python3 src/codex_pet.py --quit-after-ms 4000
```

The pose PNGs `dragon-lady.png`, `blink.png`, `wave.png` and `wave2.png` in `assets/` are
required by the app and the package. The installed files are placed alongside
the Python scripts in `/usr/share/codex-pet/`.
