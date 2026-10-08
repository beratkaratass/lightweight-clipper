# Lightweight Clipper

A replay buffer for Windows: it keeps recording your screen in the background and, when you press your hotkey, saves the last X seconds as an mp4. It can also record normally, from start to stop. Capture and encoding run on the GPU, so it costs almost no FPS.

## 1. Install ffmpeg (once)

Lightweight Clipper uses ffmpeg to record. Pick one:

**A. With winget (easiest).** Open *Terminal* or *PowerShell* and run:

```
winget install Gyan.FFmpeg.Essentials
```

**B. By hand.**

1. Download <https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip>.
2. Unzip it and open the `bin` folder.
3. Copy `ffmpeg.exe` into the same folder as `LightweightClipper.exe`.

Tested with ffmpeg 9.0.1; use a recent build.

## 2. Run it

Download `LightweightClipper.exe` from [Releases](https://github.com/beratkaratass/lightweight-clipper/releases/latest) and double-click it. Recording starts right away.

The first time, Windows SmartScreen may say *"Windows protected your PC"*, because the exe isn't code-signed. Click **More info → Run anyway**.

## Using it

- **Save a clip:** press **Alt + F10** (change it in *Clip hotkey*; any keys work, even combos like `N + M + .`). You'll hear a short chime when the clip is saved.
- **Clip length:** pick one in *Length*, or choose *Custom…* and type any number of seconds from 5 to 3600.
- **Record normally:** open the *Record* tab and press *Start recording*, or press **Alt + F9** to start and stop. Clips still work while you record.
- **Clips and recordings go to** `Videos\Lightweight Clipper` (change it under *Save to*).
- **Minimize** hides it to the tray (the **^** by the clock). Click the tray icon to bring it back; right-click it to quit.
- **Quality slider:** *Max FPS* is lightest on your game, *Max quality* looks best.
- **Skip audio of:** tick apps whose sound you don't want in clips (e.g. Discord, Spotify). Everything else is recorded, plus your mic if you pick one.
- Settings save themselves 5 s after you change them, in `config.json` next to the exe.

## Requirements

- Windows 11 (or Windows 10 build 20348+) for PC audio. Video works on any Windows 10/11.
- Any graphics card. *Encoder* is set to **Auto**: it uses NVIDIA, AMD or Intel hardware encoding, whichever works on your PC. If none does, it encodes on the CPU instead, which works everywhere but uses about one CPU core at 1440p 60 fps; the status line tells you when that happens.

## Troubleshooting

- **"ffmpeg not found"**: install ffmpeg (step 1). If you just installed it with winget and it still says that, restart your PC, or copy `ffmpeg.exe` next to the exe (option B).
- **"Screen capture interrupted, retrying"**: the screen couldn't be recorded for a moment (locked PC, sleep, a game switching display mode, an admin prompt). It resumes by itself. If it never does, the text after the dot is the reason; updating your graphics driver fixes most of them.
- **Version 1.0.0 showed "Capture interrupted, retrying … received no packets" forever** on PCs without an NVIDIA card. Download the latest release; it picks a working encoder by itself.
- **Nothing happens on the hotkey** in a game that runs as administrator: use a combo with Ctrl/Alt/Shift/Win (e.g. Alt + F10), or run Lightweight Clipper as administrator too.

## Building the exe from source

```
python -m pip install pyinstaller
python build.py
```

The exe lands in `dist\`.
