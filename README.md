# Lightweight Clipper

A replay buffer for Windows: it keeps recording your screen in the background and, when you press your hotkey, saves the last X seconds as an mp4. Capture and encoding run on the GPU, so it costs almost no FPS.

## 1. Install ffmpeg (once)

Lightweight Clipper uses ffmpeg to record. Pick one:

**A. With winget (easiest).** Open *Terminal* or *PowerShell* and run:

```
winget install Gyan.FFmpeg.Essentials
```

**B. By hand.**

1. Download <https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip>.
2. Unzip it and open the `bin` folder.
3. Copy `ffmpeg.exe` into the same folder as `Lightweight Clipper.exe`.

Tested with ffmpeg 9.0.1; use a recent build.

## 2. Run it

Double-click `Lightweight Clipper.exe`. Recording starts right away.

The first time, Windows SmartScreen may say *"Windows protected your PC"*, because the exe isn't code-signed. Click **More info → Run anyway**.

## Using it

- **Save a clip:** press **Alt + F10** (change it in *Hotkey*; any keys work, even combos like `N + M + .`). You'll hear a short chime when the clip is saved.
- **Clips go to** `Videos\Lightweight Clipper` (change it in *Save folder*).
- **Minimize** hides it to the tray (the **^** by the clock). Click the tray icon to bring it back; right-click it to quit.
- **Quality slider:** *Max FPS* is lightest on your game, *Max quality* looks best.
- **Skip audio of:** tick apps whose sound you don't want in clips (e.g. Discord, Spotify). Everything else is recorded, plus your mic if you pick one.
- Settings save themselves 5 s after you change them, in `config.json` next to the exe.

## Requirements

- Windows 11 (or Windows 10 build 20348+) for PC audio. Video works on any Windows 10/11.
- An NVIDIA (NVENC) or AMD (AMF) GPU.

## Troubleshooting

- **"ffmpeg not found"**: install ffmpeg (step 1). If you just installed it with winget and it still says that, restart your PC, or copy `ffmpeg.exe` next to the exe (option B).
- **Nothing happens on the hotkey** in a game that runs as administrator: use a combo with Ctrl/Alt/Shift/Win (e.g. Alt + F10), or run Lightweight Clipper as administrator too.

## Building the exe from source

```
python -m pip install pyinstaller
python build.py
```

The exe lands in `dist\`.
