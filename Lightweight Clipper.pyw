"""Lightweight Clipper: minimal replay buffer.
ffmpeg ddagrab (GPU desktop capture) -> GPU encoder, zero-copy (frames never touch the CPU)
-> ring of 2s .ts segments in %TEMP%. Hotkey joins the newest ones into an mp4: stream copy at Native res,
GPU re-encode only when a smaller resolution is picked."""
import ctypes, io, json, math, operator, os, re, shutil, subprocess, sys, threading, time, uuid, wave, winsound
from array import array
from ctypes import wintypes
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, font as tkfont

NAME = 'Lightweight Clipper'
APP = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent  # as an exe: its folder
CFG = APP / 'config.json'
BUF = Path(os.environ['TEMP']) / 'LightweightClipper'
SEG = 2  # seconds per ring segment = clip length granularity
# ffmpeg next to us, where winget installs it (PATH can lag behind a fresh install until sign-out), or on PATH
WINGET = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/WinGet/Packages'
FF = next((str(p) for p in [APP / 'ffmpeg.exe', *sorted(WINGET.glob('Gyan.FFmpeg*/*/bin/ffmpeg.exe'))] if p.exists()), None) \
    or shutil.which('ffmpeg') or 'ffmpeg.exe'
NOWIN = subprocess.CREATE_NO_WINDOW
user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

# (name, capture fps, Mbps, NVENC preset, AMF quality). Game-FPS cost is mostly capture fps; NVENC has its own chip.
PRESETS = [('Max FPS', 30, 8, 'p1', 'speed'), ('Light', 60, 12, 'p2', 'speed'), ('Balanced', 60, 20, 'p4', 'balanced'),
           ('High', 60, 35, 'p6', 'quality'), ('Max quality', 60, 60, 'p7', 'quality')]
DEFAULTS = dict(monitor=0, res='Native', quality=2, length=60, encoder='h264_nvenc', audio='None', exclude=[],
                hotkey=[0x38, 0x44], folder=str(Path.home() / 'Videos' / NAME))  # hotkey: Alt + F10
# Keys are physical scancodes (+0x100 when E0-prefixed), so ç/ö/. work on any layout and survive layout switches.
MOD_BITS = {0x38: 1, 0x1D: 2, 0x2A: 4, 0x15B: 8}  # Alt, Ctrl, Shift, Win -> RegisterHotKey modifier flags
NORMALIZE = {0x36: 0x2A, 0x11D: 0x1D, 0x138: 0x38, 0x15C: 0x15B}  # right Shift/Ctrl/Alt/Win count as left
PAUSE = 0x1E1  # Pause arrives E1-prefixed; give it an id of its own
BG, FIELD, LINE, HOVER, FG, MUTED, DIM = '#000000', '#111111', '#262626', '#555555', '#ffffff', '#8c8c8c', '#4a4a4a'
FAM = 'Segoe UI'  # theme() upgrades to Segoe UI Variable on Win11


def chime():
    """'Clip saved' sound: two rising bell notes (A5, E6), rendered to an in-memory WAV."""
    rate, out = 44100, array('h')
    for i in range(int(rate * .45)):
        t, s = i / rate, 0.0
        for start, hz in ((0, 880), (.09, 1318.5)):
            u = t - start
            if u >= 0:  # 5 ms attack, exponential decay, a soft octave overtone for the bell color
                s += min(u / .005, 1) * math.exp(-9 * u) * (math.sin(2 * math.pi * hz * u) + .25 * math.sin(4 * math.pi * hz * u))
        out.append(int(max(-1, min(1, .3 * s)) * 32767))
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(out.tobytes())
    return buf.getvalue()


CHIME = chime()


def rgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def paint(w, h, color_at):
    """4x4 supersampled PhotoImage: anti-aliased shapes without Pillow."""
    img, rows = tk.PhotoImage(width=w, height=h), []
    for y in range(h):
        row = []
        for x in range(w):
            s = [color_at(x + (i + .5) / 4, y + (j + .5) / 4) for i in range(4) for j in range(4)]
            row.append('#%02x%02x%02x' % tuple(round(sum(c[k] for c in s) / 16) for k in range(3)))
        rows.append('{' + ' '.join(row) + '}')
    img.put(' '.join(rows))
    return img


def rounded(fill, edge=None, size=24, r=8, bg=BG, pill=False):
    """Rounded rect with a 1px edge, blended against bg. pill: white marker at the left edge (current choice)."""
    fill, edge, bg, fg = rgb(fill), rgb(edge or fill), rgb(bg), rgb(FG)
    k = size / 2 - r  # half-length of the straight sides
    inside = lambda x, y, inset: math.hypot(max(abs(x - size / 2) - k, 0), max(abs(y - size / 2) - k, 0)) <= r - inset
    def at(x, y):
        if pill and math.hypot(x - 2.5, y - min(max(y, 9), 15)) <= 1.5:
            return fg
        return fill if inside(x, y, 1) else edge if inside(x, y, 0) else bg
    return paint(size, size, at)


def chevron(color, up=False):
    """12x7 'v' (or '^') glyph + 10px right margin, on FIELD."""
    col, bg = rgb(color), rgb(FIELD)
    def at(x, y):
        y = 7 - y if up else y
        x = 6 - abs(x - 6)  # mirror: both arms are the segment (1,1)-(6,6)
        t = min(max((x + y - 2) / 2, 0), 5)
        return col if math.hypot(x - 1 - t, y - 1 - t) <= .8 else bg
    return paint(22, 8, at)


def thumb(focus):
    """Slider handle: white disc, black gap around it, gray ring when focused."""
    fg, ring, bg = rgb(FG), rgb(MUTED), rgb(BG)
    def at(x, y):
        d = math.hypot(x - 12, y - 12)
        return fg if d <= 7 else ring if focus and 9.5 <= d <= 11 else bg
    return paint(24, 24, at)


def seg(x, y, ax, ay, bx, by):  # distance from (x, y) to a line segment
    t = min(max(((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / ((bx - ax) ** 2 + (by - ay) ** 2), 0), 1)
    return math.hypot(x - ax - t * (bx - ax), y - ay - t * (by - ay))


def checkbox(on, bg):
    """16px rounded checkbox for dropdown rows, blended against the row color."""
    fill, edge, back, ink = rgb(FG), rgb(FG if on else HOVER), rgb(bg), rgb(BG)

    def at(x, y):
        q = math.hypot(max(abs(x - 8) - 4, 0), max(abs(y - 8) - 4, 0))  # 16px box, corner radius 4
        if q > 4: return back
        if on and min(seg(x, y, 4.5, 8.5, 7, 11), seg(x, y, 7, 11, 11.5, 5.5)) <= .9: return ink
        return (fill if on else back) if q <= 3 else edge
    return paint(16, 16, at)


def logo(size, icon=False):
    """App mark: trim brackets around a play button, [ > ]: a clip cut out of the video. Dark rounded tile."""
    tile, edge, white, bg = rgb('#151515'), rgb(LINE), rgb(FG), rgb(BG)
    brackets = [[(23, 17), (17, 17), (17, 47), (23, 47)], [(41, 17), (47, 17), (47, 47), (41, 47)]]
    tri, r = [(26, 22), (26, 42), (42.5, 32)], 2.5  # play triangle, corners rounded by r
    sides = [math.dist(tri[(i + 1) % 3], tri[(i + 2) % 3]) for i in range(3)]  # side opposite each vertex
    inc = [sum(s * v[k] for s, v in zip(sides, tri)) / sum(sides) for k in (0, 1)]  # incenter
    inr = abs((tri[1][0] - tri[0][0]) * (tri[2][1] - tri[0][1]) - (tri[2][0] - tri[0][0]) * (tri[1][1] - tri[0][1])) / sum(sides)
    core = [(inc[0] + (vx - inc[0]) * (inr - r) / inr, inc[1] + (vy - inc[1]) * (inr - r) / inr) for vx, vy in tri]

    def in_core(x, y):
        s = [(bx - ax) * (y - ay) - (by - ay) * (x - ax) for (ax, ay), (bx, by) in zip(core, core[1:] + core[:1])]
        return all(v >= 0 for v in s) or all(v <= 0 for v in s)

    def at(x, y):
        x, y = x * 64 / size, y * 64 / size  # designed on a 64-unit grid
        stroke = min(seg(x, y, *a, *b) for pts in brackets for a, b in zip(pts, pts[1:]))
        play = 0 if in_core(x, y) else min(seg(x, y, *a, *b) for a, b in zip(core, core[1:] + core[:1]))
        if stroke <= 3 or play <= r:
            return white
        q = math.hypot(max(abs(x - 32) - 18, 0), max(abs(y - 32) - 18, 0))  # tile, corner radius 14
        return tile if q <= 13 else edge if q <= 14 else bg
    img = paint(size, size, at)
    if icon:  # clear the corners so the taskbar background shows through
        for y in range(size):
            for x in range(size):
                if math.hypot(max(abs(x + .5 - size / 2) - size * 18 / 64, 0), max(abs(y + .5 - size / 2) - size * 18 / 64, 0)) > size * 14.5 / 64:
                    img.transparency_set(x, y, True)
    return img


def theme(root):
    global FAM
    FAM = 'Segoe UI Variable Text' if 'Segoe UI Variable Text' in tkfont.families(root) else 'Segoe UI'
    root.configure(bg=BG)
    root.option_add('*Label.font', f'{{{FAM}}} 10')  # plain tk Labels (dropdown rows); ttk ones use the style
    s = ttk.Style(root)
    s.theme_use('clam')  # the only built-in theme that honors custom colors and layouts
    s.configure('.', background=BG, foreground=FG, font=(FAM, 10))
    s.configure('TLabel', foreground=MUTED)
    s.configure('Title.TLabel', foreground=FG, font=(FAM, 20, 'bold'))
    s.configure('Section.TLabel', foreground=DIM, font=(FAM, 8, 'bold'))
    s.configure('Status.TLabel', foreground=MUTED)
    keep = root._imgs = []  # Tk blanks PhotoImages once Python garbage-collects them

    def element(name, default, *states, **kw):  # states: (ttk state..., image), first match wins
        keep.extend([default, *(st[-1] for st in states)])
        s.element_create(name, 'image', default, *states, **{'border': 8, 'sticky': 'nsew', 'padding': 0, **kw})
    open_field = rounded(FIELD, FG)  # 'alternate' = its dropdown is open
    element('Round.field', rounded(FIELD, LINE), ('alternate', open_field), ('focus', open_field),
            ('hover', rounded(FIELD, HOVER)), padding=2)
    element('Row.bg', rounded(FIELD, bg=FIELD), ('active', 'selected', rounded(LINE, r=6, bg=FIELD, pill=True)),
            ('selected', rounded('#1a1a1a', r=6, bg=FIELD, pill=True)), ('active', rounded(LINE, r=6, bg=FIELD)))
    element('Round.button', rounded('#1a1a1a', LINE), ('pressed', rounded(LINE, HOVER)),
            ('active', rounded('#222222', HOVER)), ('focus', rounded('#1a1a1a', FG)))
    element('Accent.button', rounded(FG), ('pressed', rounded('#c8c8c8')), ('active', rounded('#e6e6e6')),
            ('focus', rounded('#d9d9d9')))
    element('Chevron', chevron(MUTED), ('alternate', chevron(FG, up=True)), ('hover', chevron(FG)), border=0, sticky='')
    textarea = lambda w: (f'{w}.padding', {'expand': '1', 'sticky': 'nsew', 'children': [(f'{w}.textarea', {'sticky': 'nsew'})]})
    s.layout('TEntry', [('Round.field', {'sticky': 'nsew', 'children': [textarea('Entry')]})])
    s.layout('TCombobox', [('Round.field', {'sticky': 'nsew', 'children': [
        ('Chevron', {'side': 'right', 'sticky': ''}), textarea('Combobox')]})])
    label = lambda w: (f'{w}.padding', {'sticky': 'nsew', 'children': [(f'{w}.label', {'sticky': 'nsew'})]})
    for style, el in (('TButton', 'Round.button'), ('Accent.TButton', 'Accent.button'), ('Field.TButton', 'Round.field')):
        s.layout(style, [(el, {'sticky': 'nsew', 'children': [label('Button')]})])
    s.layout('Row.TLabel', [('Row.bg', {'sticky': 'nsew', 'children': [label('Label')]})])
    s.configure('Row.TLabel', foreground=FG, padding=(12, 6))
    s.configure('Field.TButton', anchor='w', padding=(10, 7))  # hotkey picker, looks like an entry
    for w in ('TEntry', 'TCombobox'):
        s.configure(w, foreground=FG, insertcolor=FG, selectbackground=LINE, selectforeground=FG, padding=(10, 7))
        s.map(w, foreground=[('readonly', FG)], selectbackground=[('readonly', FIELD)])
    s.configure('TButton', padding=(14, 8), anchor='center')
    s.configure('Accent.TButton', foreground=BG, font=(FAM, 10, 'bold'), padding=(24, 11))
    s.configure('Big.TButton', font=(FAM, 10, 'bold'), padding=(24, 11))  # same size as Accent, dark look
    global CHECKS  # image specs for checkbox rows: plain, and on the hover highlight
    CHECKS = {on: (checkbox(on, FIELD), 'active', checkbox(on, LINE)) for on in (False, True)}
    keep.extend(img for spec in CHECKS.values() for img in spec[::2])


def popup(cb, checked=None, toggled=None, label=str):
    """Dropdown list with padded rows, hover highlight and Win11 rounded corners (the stock Tk listbox can't).
    With a `checked` set: checkbox rows that toggle membership and keep the list open."""
    vals = [str(v) for v in cb['values']]
    top = tk.Toplevel(cb, bg=FIELD, padx=4, pady=4)
    top.overrideredirect(True)
    multi = checked is not None
    rows = [ttk.Label(top, text=('   ' if multi else '') + label(v), style='Row.TLabel', compound='left') for v in vals]

    def hi(i):
        top.i = i
        for j, r in enumerate(rows): r.state(['active' if j == i else '!active'])

    def mark(i):
        rows[i].configure(image=CHECKS[vals[i] in checked])

    def pick(i):
        if multi:
            checked.symmetric_difference_update({vals[i]})
            mark(i)
            return toggled()
        cb.set(vals[i])
        top.destroy()
        cb.focus_set()
    for i, r in enumerate(rows):
        r.pack(fill='x', pady=1)
        r.bind('<Enter>', lambda e, i=i: hi(i))
        r.bind('<ButtonRelease-1>', lambda e, i=i: pick(i))
    cur = vals.index(cb.get()) if cb.get() in vals else -1
    if multi:
        for i in range(len(rows)): mark(i)
        top.bind('<space>', lambda e: pick(top.i))
    elif cur >= 0:
        rows[cur].state(['selected'])
    hi(max(cur, 0))
    cb.state(['alternate'])  # field shows as open: white edge, chevron flips up
    top.bind('<Destroy>', lambda e: cb.winfo_exists() and cb.state(['!alternate']))
    for key, fn in (('<Up>', lambda e: hi(max(top.i - 1, 0))), ('<Down>', lambda e: hi(min(top.i + 1, len(rows) - 1))),
                    ('<Return>', lambda e: pick(top.i)), ('<Escape>', lambda e: top.destroy()),
                    ('<FocusOut>', lambda e: (setattr(cb, 'closed_at', time.monotonic()), top.destroy()))):
        top.bind(key, fn)
    top.update_idletasks()
    top.geometry(f'{cb.winfo_width()}x{top.winfo_reqheight()}+{cb.winfo_rootx()}+{cb.winfo_rooty() + cb.winfo_height() + 4}')
    hwnd = user32.GetParent(top.winfo_id())
    dwm(hwnd, 33, 2)  # rounded corners (Win11)
    dwm(hwnd, 34, 0x262626)  # border color
    top.focus_force()


def just_closed(cb):
    """Clicking the field of an open list closes it: the click's focus change already did, so don't reopen."""
    return time.monotonic() - getattr(cb, 'closed_at', 0) < .3


def open_on_click(e):
    if not just_closed(e.widget):
        e.widget.focus_set()
        popup(e.widget)
    return 'break'  # never let Tk's stock popdown open


class Slider(tk.Canvas):
    """Snapping preset slider (ttk.Scale can't be styled like this): white fill, round thumb, stop names below."""
    PAD, Y = 40, 12

    def __init__(self, master, var, names):
        super().__init__(master, height=48, bg=BG, highlightthickness=0, takefocus=1)
        self.var, self.names = var, names
        self.thumbs = [thumb(False), thumb(True)]
        self.bind('<Button-1>', self.click)
        self.bind('<B1-Motion>', self.click)
        self.bind('<Left>', lambda e: self.set(var.get() - 1))
        self.bind('<Right>', lambda e: self.set(var.get() + 1))
        for ev in ('<Configure>', '<FocusIn>', '<FocusOut>'):
            self.bind(ev, lambda e: self.draw())

    def x(self, i):
        return self.PAD + i * (self.winfo_width() - 2 * self.PAD) / (len(self.names) - 1)

    def click(self, e):
        self.focus_set()
        self.set(min(range(len(self.names)), key=lambda i: abs(self.x(i) - e.x)))

    def set(self, i):
        i = min(max(i, 0), len(self.names) - 1)
        if i != self.var.get():
            self.var.set(i)
        self.draw()

    def draw(self):
        self.delete('all')
        v, y, last = self.var.get(), self.Y, len(self.names) - 1
        self.create_line(self.x(0), y, self.x(last), y, width=4, fill=LINE, capstyle='round')
        self.create_line(self.x(0), y, self.x(v), y, width=4, fill=FG, capstyle='round')
        for i, name in enumerate(self.names):
            if i != v:  # stop markers: notches on the filled part, dots on the rest
                self.create_oval(self.x(i) - 2, y - 2, self.x(i) + 2, y + 2, fill=BG if i < v else HOVER, outline='')
            self.create_text(self.x(i), y + 24, text=name, fill=FG if i == v else MUTED, font=(FAM, 9))
        self.create_image(self.x(v), y, image=self.thumbs[self.tk.call('focus') == str(self)])


def dwm(hwnd, attr, val):
    ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(val)), 4)


def dark_titlebar(root):
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id())
    for attr, val in ((20, 1), (35, 0x000000), (34, 0x262626)):  # dark mode, caption color, border color (Win11)
        dwm(hwnd, attr, val)
    root._icons = [logo(16, icon=True), logo(32, icon=True), logo(48, icon=True)]
    root.iconphoto(True, *root._icons)


def vcall(obj, idx, argtypes, *args):
    """Call slot idx of a COM object's vtable."""
    fn = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0][idx]
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(fn)(obj, *args)


def monitors():
    """Outputs of DXGI adapter 0: the exact list and order ddagrab's output_idx indexes."""
    class OutputDesc(ctypes.Structure):
        _fields_ = [('name', ctypes.c_wchar * 32), ('rect', wintypes.RECT), ('attached', wintypes.BOOL),
                    ('rotation', ctypes.c_uint), ('monitor', ctypes.c_void_p)]

    class DisplayDevice(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('name', ctypes.c_wchar * 32), ('string', ctypes.c_wchar * 128),
                    ('flags', wintypes.DWORD), ('id', ctypes.c_wchar * 128), ('key', ctypes.c_wchar * 128)]
    iid = (ctypes.c_char * 16).from_buffer_copy(uuid.UUID('770aae78-f26f-4dba-a829-253c83d1b387').bytes_le)  # IDXGIFactory1
    factory, adapter, out = ctypes.c_void_p(), ctypes.c_void_p(), []
    if ctypes.windll.dxgi.CreateDXGIFactory1(iid, ctypes.byref(factory)) < 0 or \
            vcall(factory, 7, [ctypes.c_uint, ctypes.c_void_p], 0, ctypes.byref(adapter)) < 0:  # EnumAdapters(0)
        return out
    while True:
        o, d, dd = ctypes.c_void_p(), OutputDesc(), DisplayDevice(cb=ctypes.sizeof(DisplayDevice))
        if vcall(adapter, 7, [ctypes.c_uint, ctypes.c_void_p], len(out), ctypes.byref(o)) < 0:  # EnumOutputs
            break
        vcall(o, 7, [ctypes.c_void_p], ctypes.byref(d))  # GetDesc
        vcall(o, 2, [])  # Release
        user32.EnumDisplayDevicesW(d.name, 0, ctypes.byref(dd), 0)  # monitor model name
        vendor = dd.id.split('\\')[1][:3] if dd.id.count('\\') else 'Unknown'  # PnP id, e.g. MONITOR\AOCB322\...
        name = dd.string if dd.string not in ('', 'Generic PnP Monitor') else f'{vendor} display'
        r = d.rect
        label = f'{name} · {r.right - r.left}×{r.bottom - r.top}' + (' · main' if (r.left, r.top) == (0, 0) else '')
        out.append(label + f' #{len(out) + 1}' if label in out else label)
    vcall(adapter, 2, [])
    vcall(factory, 2, [])
    return out


def scan_vk(code):  # physical key -> virtual-key in the current layout
    return 0x13 if code == PAUSE else user32.MapVirtualKeyW((code & 0xFF) | (0xE000 if code & 0x100 else 0), 3)


def key_label(code):  # localized name from the keyboard layout: 'Ç', '.', 'Alt', 'F10'...
    if code == PAUSE: return 'Pause'
    buf = ctypes.create_unicode_buffer(32)
    return buf.value if user32.GetKeyNameTextW(((code & 0xFF) << 16) | ((code & 0x100) << 16), buf, 32) else f'Key {code:#x}'


def stem(exe):  # 'Discord.exe' -> 'Discord'
    return exe[:-4] if exe.lower().endswith('.exe') else exe


def pretty(combo):
    return ' + '.join(map(key_label, combo))


def legacy_hotkey(s):
    """Old config format ('alt+F10', 'ctrl+shift+K', 'N') -> scancodes, None if unreadable."""
    *mods, key = s.split('+')
    vk = ord(key.upper()) if len(key) == 1 else 0x6F + int(key[1:]) if re.fullmatch(r'[Ff]\d{1,2}', key) else 0
    names = {'alt': 0x38, 'ctrl': 0x1D, 'shift': 0x2A, 'win': 0x15B}
    return [names[m.lower()] for m in mods] + [user32.MapVirtualKeyW(vk, 0)] if vk and all(m.lower() in names for m in mods) else None


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [('page', wintypes.USHORT), ('usage', wintypes.USHORT), ('flags', wintypes.DWORD), ('target', wintypes.HWND)]


class RAWINPUT(ctypes.Structure):  # RAWINPUTHEADER + RAWKEYBOARD, the only device type we register
    _fields_ = [('type', wintypes.DWORD), ('size', wintypes.DWORD), ('device', wintypes.HANDLE), ('wparam', wintypes.WPARAM),
                ('make', wintypes.USHORT), ('flags', wintypes.USHORT), ('reserved', wintypes.USHORT), ('vkey', wintypes.USHORT),
                ('message', wintypes.UINT), ('extra', wintypes.ULONG)]


RAW_HEADER = RAWINPUT.make.offset
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]


class Keys(threading.Thread):
    """All hotkey input, on one thread with a message-only window.
    Modifiers + one key -> RegisterHotKey: Windows eats the combo, and it works even over games running as admin.
    Anything else (single keys, n + m + .) -> Raw Input: sees every key system-wide without a hook, so it adds no
    input lag and keys still reach the game; but Windows hides keys typed into admin windows from it."""
    WINDOW = .6  # seconds: combo keys pressed this close together count as one chord

    def __init__(self, fire, status, captured):
        super().__init__(daemon=True)
        self.fire, self.status, self.captured = fire, status, captured
        self.combo, self.capture, self.held, self.last, self.last_press = [], None, set(), {}, 0
        self.tid, self.hwnd, self.ready = 0, None, threading.Event()

    def update(self):  # UI thread changed combo/capture: re-arm on our own thread (hotkeys belong to a thread)
        user32.PostThreadMessageW(self.tid, 0x8001, 0, 0)

    def run(self):
        self.hwnd = user32.CreateWindowExW(0, 'STATIC', None, 0, 0, 0, 0, 0, -3, None, None, None)  # -3: HWND_MESSAGE
        self.tid = kernel32.GetCurrentThreadId()
        self.ready.set()
        msg, ri, size = wintypes.MSG(), RAWINPUT(), wintypes.UINT()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x8001:
                self.arm()
            elif msg.message == 0x0312:  # WM_HOTKEY
                self.fire()
            elif msg.message == 0x00FF:  # WM_INPUT
                size.value = ctypes.sizeof(ri)
                if user32.GetRawInputData(msg.lParam, 0x10000003, ctypes.byref(ri), ctypes.byref(size), RAW_HEADER) > 0:
                    self.key(ri.make, ri.flags, ri.vkey, time.monotonic())
            user32.DispatchMessageW(ctypes.byref(msg))  # lets Windows clean up after WM_INPUT

    def arm(self):
        user32.UnregisterHotKey(None, 1)
        c = self.combo
        std = self.capture is None and len(c) > 1 and c[-1] not in MOD_BITS and all(k in MOD_BITS for k in c[:-1])
        registered = std and user32.RegisterHotKey(None, 1, sum(MOD_BITS[k] for k in set(c[:-1])) | 0x4000, scan_vk(c[-1]))
        raw = self.capture is not None or bool(c and not registered)  # taken by another app? Raw Input still sees it
        rid = RAWINPUTDEVICE(1, 6, 0x100 if raw else 1, self.hwnd if raw else None)  # INPUTSINK: also in background
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid))
        self.held.clear()

    def key(self, make, flags, vkey, now):
        if flags & 4:  # E1 prefix
            code = PAUSE
        elif make in (0, 0xFF) or vkey == 0xFF:  # overrun / fake companion events
            return
        else:
            code = make | (0x100 if flags & 2 else 0)
            if code in (0x12A, 0x136): return  # fake Shift Windows wraps around some extended keys
            code = NORMALIZE.get(code, code)
        if flags & 1:  # key up
            self.held.discard(code)
            return
        repeat = code in self.held and now - self.last[code] < 1.5  # typematic repeat of a held key (a missed key-up expires)
        self.held.add(code)
        self.last[code] = now
        if repeat: return
        self.last_press = now
        if self.capture is not None:
            if code == 0x01:  # Esc cancels
                self.captured(None)
            elif code not in self.capture:
                self.capture.append(code)
                self.captured(list(self.capture))
            return
        c = self.combo  # the last key triggers; the rest count if held or pressed within WINDOW, in any order
        if c and code == c[-1] and all(now - self.last.get(k, -9) <= self.WINDOW or
                                       (k in self.held and user32.GetAsyncKeyState(scan_vk(k)) & 0x8000) for k in c[:-1]):
            self.fire()


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateIconIndirect.restype = wintypes.HICON
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
ctypes.windll.gdi32.CreateBitmap.restype = wintypes.HBITMAP
TRAY_CLASS, TRAY_MSG = 'LightweightClipperTray', 0x8003  # WM_APP + 3: the notification area calls us with it


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', wintypes.UINT), ('proc', WNDPROC), ('cls_extra', ctypes.c_int), ('wnd_extra', ctypes.c_int),
                ('instance', wintypes.HINSTANCE), ('icon', wintypes.HICON), ('cursor', wintypes.HANDLE),
                ('background', wintypes.HBRUSH), ('menu', wintypes.LPCWSTR), ('name', wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('hwnd', wintypes.HWND), ('id', wintypes.UINT), ('flags', wintypes.UINT),
                ('callback', wintypes.UINT), ('icon', wintypes.HICON), ('tip', ctypes.c_wchar * 128), ('state', wintypes.DWORD),
                ('state_mask', wintypes.DWORD), ('info', ctypes.c_wchar * 256), ('version', wintypes.UINT),
                ('info_title', ctypes.c_wchar * 64), ('info_flags', wintypes.DWORD), ('guid', ctypes.c_byte * 16),
                ('balloon_icon', wintypes.HICON)]


class ICONINFO(ctypes.Structure):
    _fields_ = [('is_icon', wintypes.BOOL), ('x', wintypes.DWORD), ('y', wintypes.DWORD), ('mask', wintypes.HBITMAP),
                ('color', wintypes.HBITMAP)]


def hicon(img):
    """PhotoImage (with transparent pixels) -> Windows icon handle, for the tray."""
    w, h, px = img.width(), img.height(), bytearray()
    for y in range(h):
        for x in range(w):
            r, g, b = img.get(x, y)
            px += bytes((b, g, r, 0 if img.transparency_get(x, y) else 255))  # BGRA; alpha does the cut-out
    gdi = ctypes.windll.gdi32
    info = ICONINFO(True, 0, 0, gdi.CreateBitmap(w, h, 1, 1, bytes((w + 15) // 16 * 2 * h)), gdi.CreateBitmap(w, h, 1, 32, bytes(px)))
    return user32.CreateIconIndirect(ctypes.byref(info))


class Tray(threading.Thread):
    """Notification-area icon (the ^ overflow by the clock): click shows the window, right-click has Show / Quit.
    A hidden top-level window of our own gets its clicks, and Explorer's 'TaskbarCreated' to re-add it after a restart."""

    def __init__(self, icon, show, quit):
        super().__init__(daemon=True)
        self.icon, self.show, self.quit, self.hwnd, self.ready = icon, show, quit, None, threading.Event()
        self.proc = WNDPROC(self.wndproc)  # keep a reference: Windows calls it for as long as the window lives
        self.restarted = user32.RegisterWindowMessageW('TaskbarCreated')  # before the window exists: wndproc reads it

    def data(self):
        return NOTIFYICONDATAW(size=ctypes.sizeof(NOTIFYICONDATAW), hwnd=self.hwnd, id=1, flags=7, callback=TRAY_MSG,
                               icon=self.icon, tip=NAME)  # 7: MESSAGE | ICON | TIP

    def run(self):
        try:  # dark context menu to match the app (undocumented but long-stable uxtheme SetPreferredAppMode)
            ux = ctypes.WinDLL('uxtheme')
            ux[135](2)  # ForceDark
            ux[136]()  # FlushMenuThemes
        except (OSError, AttributeError):
            pass
        inst = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(WNDCLASSW(proc=self.proc, instance=inst, name=TRAY_CLASS)))
        self.hwnd = user32.CreateWindowExW(0, TRAY_CLASS, NAME, 0, 0, 0, 0, 0, None, None, inst, None)
        self.added = ctypes.windll.shell32.Shell_NotifyIconW(0, ctypes.byref(self.data()))  # NIM_ADD
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def wndproc(self, hwnd, msg, wp, lp):
        if msg == TRAY_MSG:
            if lp in (0x202, 0x203):  # left click / double click
                self.show()
            elif lp == 0x205:  # right click
                self.menu()
            return 0
        if msg == self.restarted:  # Explorer restarted: the icon is gone, add it again
            ctypes.windll.shell32.Shell_NotifyIconW(0, ctypes.byref(self.data()))
            return 0
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def menu(self):
        m, pt = user32.CreatePopupMenu(), wintypes.POINT()
        user32.AppendMenuW(m, 0, 1, f'Show {NAME}')
        user32.AppendMenuW(m, 0x800, 0, None)  # separator
        user32.AppendMenuW(m, 0, 2, 'Quit')
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)  # or the menu won't close when clicking elsewhere
        cmd = user32.TrackPopupMenu(m, 0x100 | 0x80 | 0x20, pt.x, pt.y, 0, self.hwnd, None)  # RETURNCMD | NONOTIFY | BOTTOMALIGN
        user32.DestroyMenu(m)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        if cmd == 1: self.show()
        elif cmd == 2: self.quit()

    def remove(self):
        ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(self.data()))  # NIM_DELETE


def audio_devices():
    try:
        r = subprocess.run([FF, '-hide_banner', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy'],
                           capture_output=True, text=True, errors='ignore', creationflags=NOWIN)
    except OSError:
        return []
    return re.findall(r'"([^"]+)" \(audio\)', r.stderr)


# ---- PC audio: WASAPI process loopback (Win10 20348+ / Win11) -> named pipe -> ffmpeg ----
PIPE = r'\\.\pipe\LightweightClipper.audio'
INVALID = wintypes.HANDLE(-1).value
ole32, mmdevapi = ctypes.windll.ole32, ctypes.windll.mmdevapi
k32 = ctypes.WinDLL('kernel32', use_last_error=True)  # own instance: typed signatures, reliable GetLastError
for fn, res, args in (('CreateNamedPipeW', wintypes.HANDLE, [wintypes.LPCWSTR] + [wintypes.DWORD] * 6 + [ctypes.c_void_p]),
                      ('ConnectNamedPipe', wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
                      ('WriteFile', wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]),
                      ('CreateFileW', wintypes.HANDLE, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                                        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]),
                      ('CloseHandle', wintypes.BOOL, [wintypes.HANDLE]),
                      ('CreateToolhelp32Snapshot', wintypes.HANDLE, [wintypes.DWORD, wintypes.DWORD]),
                      ('Process32FirstW', wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
                      ('Process32NextW', wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p])):
    getattr(k32, fn).restype, getattr(k32, fn).argtypes = res, args
mmdevapi.ActivateAudioInterfaceAsync.argtypes = [wintypes.LPCWSTR] + [ctypes.c_void_p] * 4
guid = lambda s: (ctypes.c_char * 16).from_buffer_copy(uuid.UUID(s).bytes_le)
IID_AUDIO_CLIENT, IID_CAPTURE_CLIENT = guid('1cb9ad4c-dbfa-4c32-b178-c2f568a703b2'), guid('c8adbd64-e71e-48a0-a4de-185c395cd317')
CLSID_ENUMERATOR, IID_ENUMERATOR = guid('bcde0395-e52f-467c-8e3d-c4579291692e'), guid('a95664d2-9614-4f35-a746-de8db63617e6')
IID_SESSIONS, IID_SESSION2 = guid('77aa99a0-1bd6-484f-8bc7-2c654c9a9b6f'), guid('bfb7ff88-7239-4fc9-8fa2-07c950be9c6d')


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('tag', wintypes.WORD), ('channels', wintypes.WORD), ('rate', wintypes.DWORD), ('bytes_per_sec', wintypes.DWORD),
                ('align', wintypes.WORD), ('bits', wintypes.WORD), ('extra', wintypes.WORD)]


class BLOBVARIANT(ctypes.Structure):  # PROPVARIANT holding a VT_BLOB
    _fields_ = [('vt', wintypes.USHORT), ('reserved', wintypes.USHORT * 3), ('size', wintypes.ULONG), ('data', ctypes.c_void_p)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD), ('heap', ctypes.c_size_t),
                ('module', wintypes.DWORD), ('threads', wintypes.DWORD), ('ppid', wintypes.DWORD), ('prio', ctypes.c_long),
                ('flags', wintypes.DWORD), ('exe', ctypes.c_wchar * 260)]


def processes():  # [(pid, parent pid, exe name)]
    snap, e, out = k32.CreateToolhelp32Snapshot(2, 0), PROCESSENTRY32W(size=ctypes.sizeof(PROCESSENTRY32W)), []
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        out.append((e.pid, e.ppid, e.exe))
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return out


def session_pids():
    """PIDs owning a live (not expired) audio session on any active output device."""
    ole32.CoInitializeEx(None, 2)  # no-op if this thread already has COM
    en, devices, count, pids = ctypes.c_void_p(), ctypes.c_void_p(), wintypes.UINT(), set()
    if ole32.CoCreateInstance(CLSID_ENUMERATOR, None, 23, IID_ENUMERATOR, ctypes.byref(en)) < 0:
        return pids
    if (vcall(en, 3, [ctypes.c_int, wintypes.DWORD, ctypes.c_void_p], 0, 1, ctypes.byref(devices)) >= 0 and  # render, ACTIVE
            vcall(devices, 3, [ctypes.c_void_p], ctypes.byref(count)) >= 0):
        for d in range(count.value):
            dev, mgr, sessions, n = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_int()
            if (vcall(devices, 4, [wintypes.UINT, ctypes.c_void_p], d, ctypes.byref(dev)) >= 0 and
                    vcall(dev, 3, [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p],
                          IID_SESSIONS, 23, None, ctypes.byref(mgr)) >= 0 and  # Activate(IAudioSessionManager2)
                    vcall(mgr, 5, [ctypes.c_void_p], ctypes.byref(sessions)) >= 0 and  # GetSessionEnumerator
                    vcall(sessions, 3, [ctypes.c_void_p], ctypes.byref(n)) >= 0):
                for i in range(n.value):
                    ctl, ctl2, pid, state = ctypes.c_void_p(), ctypes.c_void_p(), wintypes.DWORD(), ctypes.c_int()
                    if vcall(sessions, 4, [ctypes.c_int, ctypes.c_void_p], i, ctypes.byref(ctl)) < 0: continue
                    vcall(ctl, 3, [ctypes.c_void_p], ctypes.byref(state))  # GetState: 2 = expired
                    if state.value != 2 and vcall(ctl, 0, [ctypes.c_void_p, ctypes.c_void_p], IID_SESSION2, ctypes.byref(ctl2)) >= 0:
                        vcall(ctl2, 14, [ctypes.c_void_p], ctypes.byref(pid))  # GetProcessId
                        vcall(ctl2, 2, [])
                        pids.add(pid.value)
                    vcall(ctl, 2, [])
            for p in (sessions, mgr, dev):
                if p.value: vcall(p, 2, [])
        vcall(devices, 2, [])
    vcall(en, 2, [])
    pids.discard(0)  # system sounds
    return pids


def audio_apps():
    """Exe names of apps with audio sessions: the ones worth offering to skip."""
    names, pids = {pid: exe for pid, _, exe in processes()}, session_pids() - {os.getpid()}
    return sorted({names[p] for p in pids if p in names}, key=str.lower)


class ActivationHandler:
    """Minimal agile COM object: ActivateAudioInterfaceAsync reports completion through it."""
    IIDS = {uuid.UUID(s).bytes_le for s in ('00000000-0000-0000-c000-000000000046',   # IUnknown
                                              '41d949ab-9862-444a-80f6-c261334da5eb',   # IActivateAudioInterfaceCompletionHandler
                                              '94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90')}  # IAgileObject: callable from any thread

    def __init__(self):
        self.done = threading.Event()

        def qi(this, iid, out):
            ok = ctypes.string_at(iid, 16) in self.IIDS
            out[0] = this if ok else None
            return 0 if ok else -2147467262  # E_NOINTERFACE
        ref = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(lambda this: 1)  # AddRef/Release: Python owns it
        self.fns = [ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(qi), ref, ref,
                    ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(lambda this, op: self.done.set() or 0)]
        self.vtbl = (ctypes.c_void_p * 4)(*(ctypes.cast(f, ctypes.c_void_p) for f in self.fns))
        self.obj = ctypes.c_void_p(ctypes.addressof(self.vtbl))
        self.ptr = ctypes.addressof(self.obj)


def loopback_client(pid, include=False):
    """WASAPI capture of every app except pid's process tree (or only that tree), as 48 kHz float stereo."""
    params = (wintypes.DWORD * 3)(1, pid, 0 if include else 1)  # PROCESS_LOOPBACK, target, INCLUDE/EXCLUDE_TARGET_PROCESS_TREE
    pv = BLOBVARIANT(65, (0, 0, 0), ctypes.sizeof(params), ctypes.addressof(params))  # 65: VT_BLOB
    handler, op, res, client, cap = ActivationHandler(), ctypes.c_void_p(), ctypes.c_long(), ctypes.c_void_p(), ctypes.c_void_p()
    hr = mmdevapi.ActivateAudioInterfaceAsync('VAD\\Process_Loopback', IID_AUDIO_CLIENT, ctypes.byref(pv), handler.ptr, ctypes.byref(op))
    if hr < 0 or not handler.done.wait(5):
        raise OSError(f'activation failed ({hr & 0xFFFFFFFF:#x})')
    vcall(op, 3, [ctypes.c_void_p, ctypes.c_void_p], ctypes.byref(res), ctypes.byref(client))  # GetActivateResult
    vcall(op, 2, [])
    if res.value < 0:
        raise OSError(f'activation failed ({res.value & 0xFFFFFFFF:#x})')
    fmt = WAVEFORMATEX(3, 2, 48000, 384000, 8, 32, 0)  # 3: IEEE float, what the engine mixes in anyway
    hr = vcall(client, 3, [ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p],
               0, 0x88020000, 2_000_000, 0, ctypes.byref(fmt), None)  # shared, LOOPBACK|AUTOCONVERTPCM|SRC_DEFAULT_QUALITY, 200 ms
    if hr >= 0: hr = vcall(client, 14, [ctypes.c_void_p, ctypes.c_void_p], IID_CAPTURE_CLIENT, ctypes.byref(cap))  # GetService
    if hr >= 0: hr = vcall(client, 10, [])  # Start
    if hr < 0:
        vcall(client, 2, [])
        raise OSError(f'audio init failed ({hr & 0xFFFFFFFF:#x})')
    return client, cap


def release_client(client, cap):
    vcall(client, 11, [])  # Stop
    vcall(cap, 2, [])
    vcall(client, 2, [])


def mix(chunks):  # sum float PCM; only runs while 2+ recorded apps make sound at the same moment
    total = array('f', chunks[0])
    for ch in chunks[1:]:
        total = array('f', map(operator.add, total, array('f', ch)))
    return total.tobytes()


class AppAudio(threading.Thread):
    """PC audio minus the apps in `skip` (exe names), fed to ffmpeg through a named pipe.
    0-1 skipped apps running: one client that Windows filters itself (exclude mode, cheapest).
    2+: one include-mode client per other app with audio, mixed here.
    Paced by the wall clock from the moment ffmpeg opens the pipe (right after it starts the video), so A/V stay in sync."""
    FRAME, LAG = 8, .04  # float stereo bytes per frame, timeline slack for late packets
    PRIME, MAX_FIFO = 48000 * 8 * 3 // 100, 48000 * 8 * 3 // 10  # per-app cushion 30 ms (no clicks from late packets), cap 300 ms

    def __init__(self, skip, status):
        super().__init__(daemon=True)
        self.skip, self.status, self.stopped = skip, status, False
        self.pipe = k32.CreateNamedPipeW(PIPE, 2, 0, 255, 1 << 20, 0, 0, None)  # outbound, byte stream, 1 MB buffer

    def plan(self):
        """Clients to run: {('x', pid)} = everything but pid's tree, or {('i', pid), ...} = only these trees."""
        procs = processes()
        parent, name, skip = {p: pp for p, pp, _ in procs}, {p: n.lower() for p, _, n in procs}, {s.lower() for s in self.skip}
        roots = [p for p in name if name[p] in skip and name.get(parent[p]) != name[p]]
        if len(roots) <= 1:
            return {('x', roots[0] if roots else os.getpid())}  # our own tree plays nothing but save beeps

        def lineage(p):  # p and its ancestors
            chain = []
            while p in name and p not in chain and len(chain) < 32:
                chain.append(p)
                p = parent[p]
            return chain
        keep = [p for p in session_pids() if p in name and os.getpid() not in lineage(p) and not any(name[a] in skip for a in lineage(p))]
        return {('i', p) for p in keep if not any(a in keep for a in lineage(p)[1:])}  # a parent's tree already covers it

    def stop(self):
        self.stopped = True
        h = k32.CreateFileW(PIPE, 0x80000000, 0, None, 3, 0, None)  # frees a thread still waiting for ffmpeg to connect
        if h != INVALID: k32.CloseHandle(h)

    def run(self):
        ole32.CoInitializeEx(None, 0)  # MTA
        if not k32.ConnectNamedPipe(self.pipe, None) and ctypes.get_last_error() != 535:  # 535: connected already
            return k32.CloseHandle(self.pipe)
        clients, t0, sent, replan, B = {}, time.monotonic(), 0, 0, self.FRAME  # key -> [client, capture, fifo, primed]
        n, data, flags, wrote = wintypes.UINT(), ctypes.c_void_p(), wintypes.DWORD(), wintypes.DWORD()
        try:
            while not self.stopped:
                now = time.monotonic()
                if now >= replan:  # follow apps launching/quitting; ffmpeg keeps going
                    replan, want = now + 1, self.plan()
                    for key in set(clients) - want:
                        release_client(*clients.pop(key)[:2])
                    for key in want - set(clients):
                        try: clients[key] = [*loopback_client(key[1], include=key[0] == 'i'), bytearray(), False]
                        except OSError as e:
                            if key[0] == 'x': self.status(f'PC audio unavailable: {e}')
                for key, (client, cap, fifo, _) in list(clients.items()):
                    while True:
                        if vcall(cap, 5, [ctypes.c_void_p], ctypes.byref(n)) < 0:  # GetNextPacketSize; device gone
                            release_client(*clients.pop(key)[:2])  # recreated on the next plan
                            break
                        if not n.value: break
                        vcall(cap, 3, [ctypes.c_void_p] * 5, ctypes.byref(data), ctypes.byref(n), ctypes.byref(flags), None, None)
                        fifo += bytes(n.value * B) if flags.value & 2 else ctypes.string_at(data, n.value * B)  # 2: SILENT
                        vcall(cap, 4, [wintypes.UINT], n.value)  # ReleaseBuffer
                    if len(fifo) > self.MAX_FIFO:  # device clock drifted ahead: drop the oldest to stay in sync
                        del fifo[:len(fifo) - self.MAX_FIFO // 3]
                due = int((now - t0 - self.LAG) * 48000) - sent  # frames the timeline is owed
                if due >= 480:
                    size, chunks = due * B, []
                    for c in clients.values():
                        if not c[3]:  # still building its cushion: silent for now
                            if len(c[2]) < self.PRIME: continue
                            c[3] = True
                        take = c[2][:size]
                        del c[2][:size]
                        if len(take) < size:  # ran dry: rebuild the cushion rather than stutter
                            c[3] = False
                        if take.count(0) != len(take):  # skip silent apps: mixing costs only while 2+ make sound
                            chunks.append(take + bytes(size - len(take)))
                    out = mix(chunks) if len(chunks) > 1 else bytes(chunks[0]) if chunks else bytes(size)
                    if not k32.WriteFile(self.pipe, out, size, ctypes.byref(wrote), None):
                        break  # ffmpeg exited
                    sent += due
                time.sleep(.01)
        finally:
            for c in clients.values():
                release_client(c[0], c[1])
            k32.CloseHandle(self.pipe)


def enc_args(c):
    _, _, br, nv_preset, amf_quality = PRESETS[c['quality']]
    speed = ['-preset', nv_preset] if 'nvenc' in c['encoder'] else ['-quality', amf_quality]
    return ['-c:v', c['encoder'], *speed, '-b:v', f'{br}M', '-maxrate', f'{br}M', '-bufsize', f'{br * 2}M']


def record_cmd(c):
    # always native: no GPU scaler in ffmpeg works with ddagrab on NVIDIA, so scaling happens at save time instead
    fps = PRESETS[c['quality']][1]
    # -xerror: if the capture dies (display mode change, lock screen, UAC), exit instead of carrying on audio-only,
    # which piled hours of audio into one segment; the watchdog then restarts capture
    cmd = [FF, '-hide_banner', '-loglevel', 'error', '-xerror', '-f', 'lavfi', '-i', f"ddagrab=output_idx={c['monitor']}:framerate={fps}"]
    audio = []
    if 'All apps' not in c['exclude']:  # PC audio from AppAudio; opened right after the video so both start together
        cmd += ['-f', 'f32le', '-ar', '48000', '-ac', '2', '-thread_queue_size', '512', '-i', PIPE]
        audio.append(f'{len(audio) + 1}:a')
    if c['audio'] != 'None':  # mic
        cmd += ['-f', 'dshow', '-audio_buffer_size', '50', '-i', f"audio={c['audio']}"]
        audio.append(f'{len(audio) + 1}:a')
    cmd += ['-map', '0:v']
    if len(audio) == 2:
        cmd += ['-filter_complex', f'[{audio[0]}][{audio[1]}]amix=inputs=2:normalize=0[a]', '-map', '[a]']
    elif audio:
        cmd += ['-map', audio[0]]
    if audio:
        cmd += ['-c:a', 'aac', '-b:a', '160k']
    return cmd + enc_args(c) + ['-g', str(fps * SEG),  # keyframe per segment so segments cut cleanly
                                '-f', 'segment', '-segment_time', str(SEG), '-reset_timestamps', '1',
                                '-segment_wrap', str(math.ceil(c['length'] / SEG) + 4), str(BUF / 'buf%03d.ts')]


def save_cmd(c, lst, out):
    concat = ['-f', 'concat', '-safe', '0', '-i', str(lst)]
    if c['res'] == 'Native':
        mid = concat + ['-c', 'copy']  # instant, no re-encode
    else:  # re-encode once at save time: NVDEC -> scale_cuda -> NVENC, all on the GPU
        w, h = c['res'].split('x')
        # ponytail: AMF decodes on the GPU but scales on the CPU; scale_d3d11 is the upgrade if AMD users show up
        nv = 'nvenc' in c['encoder']
        mid = (['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'] if nv else ['-hwaccel', 'd3d11va']) + concat + [
            '-vf', f'scale_cuda={w}:{h}' if nv else f'scale={w}:{h}', *enc_args(c), '-c:a', 'copy']
    return [FF, '-y', '-loglevel', 'error'] + mid + ['-movflags', '+faststart', str(out)]


def newest_segments(length):
    files = sorted(BUF.glob('buf*.ts'), key=lambda p: p.stat().st_mtime)
    return files[-(math.ceil(length / SEG) + 1):]  # +1: the segment still being written


class App:
    def __init__(self, root):
        self.root, self.proc, self.audio, self.started, self.capturing = root, None, None, 0, False
        # capture bookkeeping shared with supervise() threads: generation (bumped on every start/stop), quick failures
        # in a row, waiting to retry, status text for the UI thread to show
        self.gen, self.fails, self.retrying, self.note, self.lock = 0, 0, False, None, threading.RLock()
        try: saved = json.loads(CFG.read_text())
        except (OSError, ValueError): saved = {}
        self.cfg = {**DEFAULTS, **saved}
        hk = self.cfg['hotkey']
        hk = legacy_hotkey(hk) if isinstance(hk, str) else hk
        self.hotkey = hk if isinstance(hk, list) and hk and all(isinstance(k, int) for k in hk) else DEFAULTS['hotkey']
        self.cfg['hotkey'] = self.hotkey
        self.keys = Keys(lambda: threading.Thread(target=self.save_clip, daemon=True).start(),
                         lambda text: setattr(self, 'note', text), self.captured)
        self.keys.start()
        self.keys.ready.wait()
        self.monitors = monitors() or ['Display 1']
        root.title(NAME)
        root.resizable(False, False)
        theme(root)
        body = ttk.Frame(root, padding=(28, 22, 28, 22))
        body.pack(fill='both')
        body.columnconfigure((0, 1), weight=1, uniform='col')

        head = ttk.Frame(body)
        head.grid(row=0, column=0, columnspan=2, sticky='ew')
        head.columnconfigure(1, weight=1)
        self.logo = logo(48)
        ttk.Label(head, image=self.logo).grid(row=0, column=0, rowspan=2, padx=(0, 14))
        ttk.Label(head, text=NAME, style='Title.TLabel').grid(row=0, column=1, sticky='sw')
        bar = ttk.Frame(head)
        bar.grid(row=1, column=1, sticky='nw')
        self.dot = ttk.Label(bar, text='●', style='Section.TLabel')
        self.dot.pack(side='left', padx=(1, 7))
        self.status = tk.StringVar()
        ttk.Label(bar, textvariable=self.status, style='Status.TLabel', wraplength=520).pack(side='left')
        self.rec = ttk.Button(head, width=15, command=self.toggle)
        self.rec.grid(row=0, column=2, rowspan=2, sticky='e')

        ex = self.cfg['exclude']  # older configs stored one name, 'None' or 'All apps'
        self.skip = set([] if ex == 'None' else [ex]) if isinstance(ex, str) else set(ex)
        shown = {**self.cfg, 'monitor': self.monitors[min(int(self.cfg['monitor']), len(self.monitors) - 1)],
                 'exclude': self.skip_text()}
        columns = [[('CAPTURE', [('Monitor', 'monitor', self.monitors),
                                 ('Resolution', 'res', ['Native', '3840x2160', '2560x1440', '1920x1080', '1600x900', '1280x720']),
                                 ('Encoder', 'encoder', ['h264_nvenc', 'hevc_nvenc', 'h264_amf', 'hevc_amf']),
                                 ('Length (s)', 'length', [15, 30, 45, 60, 90, 120, 180, 300, 600])])],
                   [('AUDIO', [('Mic', 'audio', ['None'] + audio_devices()),
                               ('Skip audio of', 'exclude', self.skip_values())]),  # every other app is recorded
                    ('CLIP', [('Hotkey', 'hotkey', None),
                              ('Save folder', 'folder', None)])]]
        self.vars = {}
        for col, sections in enumerate(columns):
            f = ttk.Frame(body)
            f.grid(row=1, column=col, sticky='new', padx=(0, 20) if col == 0 else (20, 0), pady=(18, 0))
            f.columnconfigure(1, weight=1)
            i = 0
            for title, fields in sections:
                ttk.Label(f, text=title, style='Section.TLabel').grid(row=i, column=0, columnspan=3, sticky='w', pady=((14 if i else 0), 6))
                for i, (label, key, values) in enumerate(fields, i + 1):
                    self.field(f, i, label, key, values, shown)
                i += 1

        q = ttk.Frame(body)
        q.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(18, 0))
        q.columnconfigure(0, weight=1)
        ttk.Label(q, text='QUALITY', style='Section.TLabel').grid(row=0, column=0, sticky='w')
        self.qinfo = ttk.Label(q)
        self.qinfo.grid(row=0, column=1, sticky='e')
        self.quality = tk.IntVar(value=min(max(int(self.cfg['quality']), 0), len(PRESETS) - 1))
        Slider(q, self.quality, [p[0] for p in PRESETS]).grid(row=1, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        self.show_preset()

        self.pending = None
        for v in self.vars.values():
            v.trace_add('write', self.changed)
        self.quality.trace_add('write', lambda *_: (self.show_preset(), self.changed()))
        dark_titlebar(root)
        root.protocol('WM_DELETE_WINDOW', self.quit)
        # minimize = tuck into the tray: no taskbar button, icon stays by the clock
        self.tray = Tray(hicon(logo(user32.GetSystemMetrics(49), icon=True)),  # 49: SM_CXSMICON, DPI-scaled
                         lambda: root.after(0, self.show_window), lambda: root.after(0, self.quit))
        self.tray.start()
        self.tray.ready.wait()
        root.bind('<Unmap>', lambda e: e.widget is root and root.state() == 'iconic' and root.withdraw())
        if self.apply():
            self.start(wipe=True)
        self.watchdog()

    def skip_values(self):  # apps using audio right now, plus saved picks that aren't running
        return sorted(set(audio_apps()) | (self.skip - {'All apps'}), key=str.lower) + ['All apps']

    def skip_text(self):
        if 'All apps' in self.skip: return 'All apps (mic only)'
        names = sorted((stem(s) for s in self.skip), key=str.lower)
        return ', '.join(names) if len(names) <= 2 else f'{names[0]}, {names[1]} +{len(names) - 2}' if names else 'Nothing'

    def open_skip(self, cb):
        if just_closed(cb):
            return 'break'
        cb.configure(values=self.skip_values())
        cb.focus_set()
        popup(cb, self.skip, lambda: (self.vars['exclude'].set(self.skip_text()), self.changed()), label=stem)
        return 'break'

    def field(self, f, i, label, key, values, shown):
        ttk.Label(f, text=label).grid(row=i, column=0, sticky='w', padx=(0, 20), pady=4)
        if key == 'hotkey':  # click, then press the keys
            w = self.hk_btn = ttk.Button(f, text=pretty(self.hotkey), style='Field.TButton', command=self.capture)
            w.bind('<KeyPress>', lambda e: 'break' if self.capturing else None)  # keys go to Raw Input, not Tk
            w.bind('<FocusOut>', lambda e: self.capturing and self.end_capture(None))
        else:
            v = self.vars[key] = tk.StringVar(value=str(shown[key]))
        if values is None and key != 'hotkey':
            w = ttk.Entry(f, textvariable=v, width=24)
        elif values is not None:
            w = ttk.Combobox(f, textvariable=v, values=values, width=42, state='readonly')
            if key == 'exclude':  # checkbox list
                w.bind('<Button-1>', lambda e: self.open_skip(e.widget))
                w.bind('<Down>', lambda e: self.open_skip(e.widget))
            else:
                w.bind('<Button-1>', open_on_click)
                w.bind('<Down>', lambda e: popup(e.widget) or 'break')
            w.bind('<MouseWheel>', lambda e: 'break')  # stock binding cycles values on scroll
        w.grid(row=i, column=1, columnspan=1 if key == 'folder' else 2, sticky='ew', pady=4)
        if key == 'folder':
            ttk.Button(f, text='Browse', width=7, command=lambda: v.set(filedialog.askdirectory() or v.get())).grid(row=i, column=2, padx=(8, 0))


    def show_preset(self):
        _, fps, br, *_ = PRESETS[self.quality.get()]
        self.qinfo.configure(text=f'{fps} fps  ·  {br} Mbps')

    def changed(self, *_):  # debounce: apply 5s after the last edit
        if self.pending:
            self.root.after_cancel(self.pending)
        self.pending = self.root.after(5000, self.apply)
        self.status.set('Applying changes in 5s')

    def toggle(self):
        if self.pending:  # apply edits right away instead of waiting out the timer
            self.root.after_cancel(self.pending)
            if not self.apply(): return
        if self.proc or self.retrying:
            self.stop()
        else:
            self.start(wipe=True)
        self.sync_ui()

    def capture(self):
        self.capturing, self.keys.capture = True, []
        self.keys.update()  # drops our registered hotkey (Windows would swallow it) and listens via Raw Input
        self.hk_btn.configure(text='Press your keys · Esc cancels')
        self.hk_btn.focus_set()

    def captured(self, combo):  # from the Keys thread: live progress, None = cancelled
        if combo is None:
            return self.end_capture(None)
        self.hk_btn.configure(text=pretty(combo) + '  …')
        self.root.after(int(Keys.WINDOW * 1000) + 50, self.finish_capture)

    def finish_capture(self):  # done once every key is up and no new one came within WINDOW
        k = self.keys
        if not self.capturing or not k.capture:
            return
        if k.held or time.monotonic() - k.last_press < k.WINDOW:
            return self.root.after(100, self.finish_capture)
        self.end_capture(list(k.capture))

    def end_capture(self, combo):
        self.capturing, self.keys.capture = False, None
        if combo and combo != self.hotkey:
            self.hotkey = combo
            self.changed()  # saved with the other settings
        self.hk_btn.configure(text=pretty(self.hotkey))
        self.keys.combo = self.hotkey
        self.keys.update()  # re-arm right away

    def state_text(self):
        return f"Recording · {pretty(self.cfg['hotkey'])} saves the last {self.cfg['length']}s" if self.proc else 'Stopped'

    def show_state(self):
        self.status.set(self.state_text())
        self.refresh()

    def sync_ui(self):  # show what the background threads left in self.note (they never touch Tk themselves)
        note, self.note = self.note, None
        if note:
            self.status.set(self.state_text() if note == 'state' else note)
        self.refresh()

    def refresh(self):
        on = bool(self.proc or self.retrying)  # retrying still counts as on: the user didn't stop it
        if getattr(self, 'shown', None) != (on, bool(self.proc)):  # touch the widgets only on change
            self.shown = on, bool(self.proc)
            self.dot.configure(foreground=FG if self.proc else DIM)
            self.rec.configure(text='Stop recording' if on else 'Start recording', style='Big.TButton' if on else 'Accent.TButton')

    def apply(self):
        self.pending = None
        try:
            c = {k: v.get().strip() for k, v in self.vars.items()}
            c['monitor'] = self.monitors.index(c['monitor']) if c['monitor'] in self.monitors else 0
            c['quality'] = self.quality.get()
            c['length'] = int(c['length'])
            if c['res'] != 'Native' and not re.fullmatch(r'\d+x\d+', c['res']): raise ValueError('resolution must be WxH')
            c['hotkey'], c['exclude'] = self.hotkey, sorted(self.skip, key=str.lower)
        except (ValueError, KeyError) as e:
            return self.status.set(f'Invalid setting: {e}')
        old, self.cfg = self.cfg, c
        CFG.write_text(json.dumps(c, indent=2))
        if not self.capturing:
            self.keys.combo = self.hotkey
            self.keys.update()
        if self.audio:
            self.audio.skip = set(c['exclude'])  # picked up within a second, no ffmpeg restart
        if self.proc and record_cmd(c) != record_cmd(old):
            self.start(wipe=True)  # capture changed: restart (old segments won't join with new ones)
            self.sync_ui()
        elif self.status.get() in ('', 'Applying changes in 5s'):  # don't clobber 'Saved ...'
            self.show_state()  # res/folder/hotkey apply without touching the buffer
        return True

    def start(self, wipe=False, gen=None):
        """Launch the capture. Also runs on supervise()'s thread, so no Tk calls here: sync_ui shows the result.
        gen: only if nothing stopped or restarted the capture since that generation."""
        with self.lock:
            if gen is not None and gen != self.gen:
                return
            self.gen += 1  # first: the old supervisor must see its capture was replaced, not crashed
            self.halt()
            BUF.mkdir(exist_ok=True)
            if wipe:
                for f in BUF.glob('buf*.ts'): f.unlink()
            if 'All apps' not in self.cfg['exclude']:  # the pipe must exist before ffmpeg tries to open it
                self.audio = AppAudio(set(self.cfg['exclude']), lambda text: setattr(self, 'note', text))
                self.audio.start()
            with open(BUF / 'ffmpeg.log', 'w') as log:
                try:
                    self.proc = subprocess.Popen(record_cmd(self.cfg), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                 stderr=log, creationflags=NOWIN)
                except OSError:
                    self.halt()
                    self.note = f'ffmpeg not found ({FF})'
                    return
            self.started, self.retrying, self.note = time.time(), False, 'state'
            threading.Thread(target=self.supervise, args=(self.proc, self.gen), daemon=True).start()

    def supervise(self, proc, gen):
        """Restart a capture that died (a game's display mode switch, lock screen, UAC) right here, not on the UI
        thread: Windows stalls that one for a second or two during mode switches."""
        proc.wait()
        with self.lock:
            if gen != self.gen:
                return  # stopped or replaced on purpose
            err = (BUF / 'ffmpeg.log').read_text(errors='ignore').strip().splitlines()
            self.fails = 1 if time.time() - self.started > 10 else self.fails + 1
            self.halt()
            self.retrying, self.note = True, 'Capture interrupted, retrying · ' + (err[-1][-90:] if err else 'unknown error')
        # right away after a healthy run (the mode switch is over once capture dies), every second while it
        # can't start yet, every 5 s after ~15 s (lock screen)
        time.sleep(0 if self.fails == 1 else 1 if self.fails < 15 else 5)
        self.start(gen=gen)  # keeps the buffer: clips just skip the gap

    def halt(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None
        if self.audio:
            self.audio.stop()
            self.audio = None

    def stop(self):
        with self.lock:
            self.gen += 1
            self.halt()
            self.retrying, self.note = False, 'state'

    def watchdog(self):
        self.sync_ui()
        self.root.after(250, self.watchdog)

    def save_clip(self):
        files = newest_segments(self.cfg['length'])
        if not (self.proc or self.retrying) or not files:  # while retrying, what came before the interruption still saves
            self.note = 'Not recording, nothing to save'
            return winsound.MessageBeep(winsound.MB_ICONHAND)
        out = Path(self.cfg['folder']) / time.strftime('Clip_%Y-%m-%d_%H-%M-%S.mp4')
        lst = BUF / f'{out.stem}.txt'
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            lst.write_text(''.join(f"file '{f.as_posix()}'\n" for f in files))
            self.note = 'Saving...'
            r = subprocess.run(save_cmd(self.cfg, lst, out), capture_output=True,
                               creationflags=NOWIN | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
            lst.unlink()
            ok, err = r.returncode == 0, r.stderr.decode(errors='ignore').strip()[-200:]
        except OSError as e:
            ok, err = False, str(e)
        if ok:  # we're on the save thread, so playing synchronously blocks nothing
            winsound.PlaySound(CHIME, winsound.SND_MEMORY)
        else:
            winsound.MessageBeep(winsound.MB_ICONHAND)
        self.note = f'Saved {out.name}' if ok else f'Save failed: {err}'

    def show_window(self):  # back from the tray
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self):
        self.tray.remove()
        self.stop()
        shutil.rmtree(BUF, ignore_errors=True)
        self.root.destroy()


if __name__ == '__main__':
    if sys.argv[1:] == ['--test']:
        fired = []
        k = Keys(lambda: fired.append(1), print, print)  # thread not started: feed key() by hand
        down, up = lambda c, t, fl=0: k.key(c & 0xFF, fl | (2 if c & 0x100 else 0), 1, t), \
            lambda c, t: k.key(c & 0xFF, 1 | (2 if c & 0x100 else 0), 1, t)
        tap = lambda c, t: (down(c, t), up(c, t + .05))
        N, M, DOT, SHIFT, RSHIFT, F10 = 0x31, 0x32, 0x34, 0x2A, 0x36, 0x44
        k.combo = [N, M, DOT]
        tap(N, 0); tap(M, .2); tap(DOT, .4)
        assert fired == [1], 'quick n, m, . fires'
        tap(N, 5); tap(M, 5.2); tap(DOT, 5.9)
        assert fired == [1], 'n and m too long before .'
        tap(DOT, 10); tap(N, 10.1); tap(M, 10.2)
        assert fired == [1], 'the last key is the trigger'
        tap(M, 20); tap(N, 20.1); down(DOT, 20.2); down(DOT, 20.25); down(DOT, 20.28); up(DOT, 20.3)
        assert fired == [1, 1], 'any order before the trigger; holding it fires once'
        k.combo = [SHIFT, F10]
        down(RSHIFT, 30); down(RSHIFT, 30.5); down(RSHIFT, 30.9); tap(F10, 31)
        assert fired == [1, 1, 1], 'right Shift counts as Shift, held (repeating) modifier counts'
        k.key(0x2A, 2, 1, 40); tap(F10, 40.1)
        assert fired == [1, 1, 1], "Windows' fake E0 Shift is ignored"
        k.capture = []
        tap(N, 50); tap(DOT, 50.1); tap(N, 50.2)
        assert k.capture == [N, DOT], 'capture records each key once, in order'
        assert legacy_hotkey('N') == [N] and legacy_hotkey('alt+F10') == [0x38, F10], 'old configs convert'
        assert legacy_hotkey('ctrl+shift+K') == [0x1D, SHIFT, 0x25] and legacy_hotkey('huh+X') is None
        mixed = array('f', mix([array('f', [.25, -.5]).tobytes(), array('f', [.5, .25]).tobytes()]))
        assert list(mixed) == [.75, -.25], 'mix sums samples'
        sys.exit(print('ok'))
    mutex = k32.CreateMutexW(None, False, 'LightweightClipper.single')  # a 2nd copy would wipe the 1st one's buffer
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: surface the running window instead (even from the tray)
        user32.AllowSetForegroundWindow(-1)  # let it take focus: we hold the right, having just been launched
        user32.PostMessageW(user32.FindWindowW(TRAY_CLASS, None), TRAY_MSG, 0, 0x202)  # as if its tray icon was clicked
        sys.exit()
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('LightweightClipper')  # own taskbar entry + icon, not Python's
    root = tk.Tk()
    App(root)
    root.mainloop()
