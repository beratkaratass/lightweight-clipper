"""Build 'dist/Lightweight Clipper.exe':  python build.py   (one-time: python -m pip install pyinstaller)"""
import importlib.machinery, struct, subprocess, sys, tempfile, tkinter as tk
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / 'Lightweight Clipper.pyw'
app = importlib.machinery.SourceFileLoader('clipper', str(SRC)).load_module()
tmp = Path(tempfile.mkdtemp(prefix='clipper-build-'))

# exe icon: the app's own logo at every size Windows asks for, packed as PNGs inside an .ico
root = tk.Tk()
root.withdraw()
pngs = []
for s in (16, 20, 24, 32, 48, 64, 256):
    f = tmp / f'{s}.png'
    app.logo(s, icon=True).write(str(f), format='png')
    pngs.append((s, f.read_bytes()))
entries, blobs, offset = b'', b'', 6 + 16 * len(pngs)
for s, png in pngs:  # ICONDIRENTRY: 0 means 256
    entries += struct.pack('<BBBBHHII', s % 256, s % 256, 0, 0, 1, 32, len(png), offset + len(blobs))
    blobs += png
(tmp / 'icon.ico').write_bytes(struct.pack('<HHH', 0, 1, len(pngs)) + entries + blobs)

subprocess.run([sys.executable, '-m', 'PyInstaller', '--onefile', '--windowed', '--noconfirm', '--name', app.NAME,
                '--icon', str(tmp / 'icon.ico'), '--distpath', str(HERE / 'dist'), '--workpath', str(tmp / 'work'),
                '--specpath', str(tmp), str(SRC)], check=True)
print('built', HERE / 'dist' / f'{app.NAME}.exe')
