# Controller Shutup

An NVDA add-on that stops NVDA's current speech whenever you press a button, trigger, d-pad direction or analog stick on a game controller, much like pressing Control on the keyboard.

## Features

- Works with Xbox and other XInput controllers (all buttons, d-pad, triggers and both analog sticks).
- Works with other controllers Windows sees as joysticks, such as PlayStation pads and generic USB gamepads (all buttons, d-pad / POV hat and all axes).
- Sticks and triggers count as pressed once pushed at least halfway.
- Only a new press interrupts speech; holding a button does not keep silencing NVDA.
- Speech that the game or NVDA starts in response to the press is kept; only speech that was already playing is stopped.
- Controllers can be connected or disconnected while NVDA is running.
- Works in the background, whatever window has focus.

## Toggling

The command "Toggles whether pressing a game controller button stops speech" can be assigned a gesture under NVDA menu → Preferences → Input Gestures → Controller Shutup. It has no gesture by default. The setting is saved in your NVDA configuration.

## Building

```
python build.py
```

This produces `controllerShutup-<version>.nvda-addon` in the repository root. The section of [CHANGELOG.md](CHANGELOG.md) for the manifest's version is copied into the packaged manifest's `changelog` key, which the Add-on Store shows (NVDA 2026.1 and later). Add a section for each new version before building.

## License

GPL-2.0, see [LICENSE](LICENSE).
