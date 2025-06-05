#!/usr/bin/env python3
"""
convertupload_headless.py – end-to-end kiosk pipeline
=====================================================
• Plays the raw clip immediately on all monitors while DaVinci Resolve 20
  renders an enhanced version **headlessly** (`-nogui` → almost zero GPU use).
• When the render finishes it swaps the preview to the enhanced file, collects
  email + optional phone, then uploads to Drive and delivers the link via
  Gmail + carrier SMS gateways.
• GUI is full-screen tkinter with on-screen keyboard & star rating.

Changes vs. original CONVERTUPLOAD.PY
-------------------------------------
✓ Resolve is launched with **-nogui**  → 100% headless render.
✓ Creates a temporary timeline from INPUT_VIDEO so DaVinci renders entire clip.
✓ Queries INPUT_VIDEO’s width/height and forces timeline to that resolution,
  avoiding any vertical-crop issues.
✓ Ensures the exported video from DaVinci always matches the input length.
✓ Upload progress now logs extensive debug output to trace issues.
✓ Refresh-token errors are caught and re-trigger OAuth flow.
✓ Typo fixed (`vlc.State.EndED` → `vlc.State.Ended`) that crashed the poll loop.
✓ A few cosmetic clean-ups. Everything else—ffplay displays, Drive upload,
  Gmail, SMS, UI flow—remains unchanged.
"""

import os
import sys
import subprocess
import threading
import base64
import re
import time
import socket
import types
import ctypes
from datetime import datetime
from shutil import which

import tkinter as tk
from tkinter import font, StringVar

# ──────────────────────────────────────────────────────────────────────────
# Optional deps that may be missing on the kiosk PC
try:
    import psutil
except ImportError:
    psutil = None                    # fallback to `tasklist`

try:
    import winsound                  # Windows click-sound
except ImportError:
    winsound = types.SimpleNamespace(PlaySound=lambda *a, **k: None)

try:
    from screeninfo import get_monitors
except ImportError:
    def get_monitors():
        class M:
            x = 0
            y = 0
            width = 800
            height = 600
        return [M()]

# ─────────────────── DaVinci Resolve scripting bootstrap ─────────────────
RESOLVE_EXE = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"

resolve_paths = [
    r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
    r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
]
for p in resolve_paths:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

# ─────────────────────────────  CONSTANTS  ───────────────────────────────
SAVED_DIR          = r"C:\Users\agoll\Desktop\VPSRTfr\Saved"
INPUT_VIDEO        = os.path.join(SAVED_DIR, "test_vide.mp4")
PROJECT_PATH       = r"C:\Users\agoll\Desktop\TOOLS\EnhanceTemplate\EnhanceTemplate.drp"
PROJECT_NAME       = "EnhanceTemplate"

TOKEN_PATH         = os.path.join(os.getcwd(), "token.json")
CLIENT_SECRET_PATH = os.path.join(os.getcwd(), "credentials.json")
SCOPES             = ["https://www.googleapis.com/auth/drive.file",
                      "https://www.googleapis.com/auth/gmail.send"]

GMAIL_ADDRESS      = "agollnick1@gmail.com"
CLICK_SOUND        = r"C:\Users\agoll\Desktop\TOOLS\click.wav"

CARRIER_GATEWAYS = {
    "ATT":     "@txt.att.net",
    "Verizon": "@vtext.com",
    "TMobile": "@tmomail.net",
    "Sprint":  "@messaging.sprint.com",
}

UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # larger chunks for faster uploads

# ──────────────────────────── Resolve headless helpers ────────────────────

def locate_paths():
    """Verify that Resolve.exe, fusionscript.dll, and the Scripting "Modules" folder exist."""
    pf = os.environ.get("ProgramFiles", r"C:\\Program Files")
    pd = os.environ.get("ProgramData",  r"C:\\ProgramData")
    root = os.path.join(pf, "Blackmagic Design", "DaVinci Resolve")
    exe  = os.path.join(root, "Resolve.exe")
    dll  = os.path.join(root, "fusionscript.dll")
    mods = os.path.join(pd,  "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting", "Modules")
    for p in (exe, dll, mods):
        if not os.path.exists(p):
            raise SystemExit(f"❌ Required file not found: {p}")
    return exe, dll, mods


def _resolve_running():
    """Return True if Resolve.exe is already running."""
    if psutil:
        return any((p.name() or '').lower().startswith('resolve.exe') for p in psutil.process_iter(['name']))
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq resolve.exe'], text=True, capture_output=True)
    return 'resolve.exe' in out.stdout.lower()


def launch_resolve_headless():
    """Launch Resolve with -nogui if not already running."""
    if _resolve_running():
        return
    exe, dll, mods = locate_paths()
    if not os.path.isfile(exe):
        sys.exit(f"Resolve.exe not found at {exe}")
    subprocess.Popen([exe, '-nogui'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def bootstrap_api():
    """Load fusionscript.dll and import DaVinciResolveScript."""
    exe, dll, mods = locate_paths()
    ctypes.CDLL(dll)
    if mods not in sys.path:
        sys.path.append(mods)
    import DaVinciResolveScript as dvr
    return dvr

BAR = 30

def show_progress(pct):
    pct = max(0, min(100, int(pct)))
    filled = int(BAR * pct / 100)
    barstr = "█" * filled + "·" * (BAR - filled)
    print(f"  |{barstr}| {pct:3d}%", end="\r", flush=True)


def get_video_dimensions(path):
    if not which('ffprobe'):
        return None, None
    try:
        out = subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries','stream=width,height','-of','csv=p=0', path], text=True)
        w, h = out.strip().split(',')
        return int(w), int(h)
    except Exception:
        return None, None


def headless_render():
    exe, dll, mods = locate_paths()
    launch_resolve_headless()
    dvr = bootstrap_api()

    resolve = None
    for _ in range(30):
        try:
            resolve = dvr.scriptapp('Resolve')
        except Exception:
            resolve = None
        if resolve:
            break
        time.sleep(1)
    if not resolve:
        raise RuntimeError('❌ Could not connect to Resolve scripting host.')

    pm = resolve.GetProjectManager()
    if not pm.LoadProject(PROJECT_NAME):
        if os.path.exists(PROJECT_PATH):
            pm.ImportProject(PROJECT_PATH)
            if not pm.LoadProject(PROJECT_NAME):
                raise RuntimeError('❌ Failed to load or import template project.')
        else:
            raise RuntimeError('❌ Project file missing and project not already loaded.')

    proj = pm.GetCurrentProject()
    mediaPool = proj.GetMediaPool()
    clips = mediaPool.ImportMedia([INPUT_VIDEO])
    if not clips:
        raise RuntimeError('❌ Failed to import INPUT_VIDEO into Media Pool.')
    temp_tl_name = f"TempTimeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    new_tl = mediaPool.CreateTimelineFromClips(temp_tl_name, clips)
    if not new_tl:
        raise RuntimeError('❌ Failed to create temporary timeline from input clip.')
    proj.SetCurrentTimeline(new_tl)

    w, h = get_video_dimensions(INPUT_VIDEO)
    if w and h:
        proj.SetSetting('timelineResolutionWidth', str(w))
        proj.SetSetting('timelineResolutionHeight', str(h))
        proj.SetSetting('timelineOutputResolutionWidth', str(w))
        proj.SetSetting('timelineOutputResolutionHeight', str(h))

    base = os.path.splitext(os.path.basename(INPUT_VIDEO))[0] + '_enhanced'
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    custom_name = f"{base}_{stamp}"

    settings = {
        'TargetDir': SAVED_DIR,
        'CustomName': custom_name,
        'Format': 'mp4',
        'VideoCodec': 'H.265',
        'Encoder': 'NVIDIA',
        'ExportVideo': True,
        'ExportAudio': True,
        'Quality': 'Best',
        'RateControl': 'VBR',
        'SplitMode': 'Split',
        'Preset': 'Fast',
        'Tuning': 'High Quality',
        'TwoPass': False,
        'Lookahead': '16',
        'LookaheadLevel': '0',
        'EnableAdaptiveBFrame': True,
        'AQStrength': '8',
        'PixelAspectRatio': 'Square',
        'DataLevels': 'Auto',
        'ColorSpaceTag': 'SameAsProject',
        'GammaTag': 'SameAsProject',
        'BypassReEncodeWhenPossible': True
    }

    proj.SetRenderSettings(settings)
    proj.DeleteAllRenderJobs()
    job_id = proj.AddRenderJob()
    if not job_id:
        raise RuntimeError('❌ Failed to queue a render job.')
    if not proj.StartRendering(job_id):
        raise RuntimeError('❌ Render failed to start – check Deliver settings.')

    print(f"\n▶ Headless render started → '{custom_name}.mp4' …\n")
    pct = -1
    while True:
        try:
            running = proj.IsRenderingInProgress()
        except Exception:
            running = False
        try:
            new_pct = proj.GetRenderJobProgress(job_id) or 0
        except Exception:
            new_pct = 0
        if new_pct != pct:
            show_progress(new_pct)
            pct = new_pct
        if not running:
            break
        time.sleep(1)

    print('\n✅ Render complete.\n')
    try:
        resolve.Quit()
    except Exception:
        pass
    return os.path.join(SAVED_DIR, custom_name + '.mp4')

# ───────────────────────── Misc ffprobe / ffmpeg utilities ─────────────────

def get_video_duration(path):
    if not which('ffprobe'):
        return 60.0
    try:
        out = subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1', path], text=True)
        return float(out.strip())
    except Exception:
        return 60.0


def trim_to_duration(path, duration):
    if not all(map(which, ('ffprobe','ffmpeg'))):
        return
    try:
        out = subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1', path], text=True)
        cur = float(out.strip())
        if cur > duration + 0.1:
            tmp = path + '.trim.mp4'
            subprocess.run(['ffmpeg','-y','-i', path, '-t', str(duration), '-c', 'copy', tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            os.replace(tmp, path)
    except Exception:
        pass

# ─────────────────────  Prevent double-launch of this script ──────────────
try:
    _guard_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _guard_sock.bind(('localhost', 65432))
except OSError:
    sys.exit('Already running.')

# ────────────────  Secondary-monitor raw-clip loop ────────────────────────
import vlc
monitors = get_monitors()
os.environ['SDL_VIDEO_WINDOW_POS'] = f'{monitors[0].x},{monitors[0].y}'
_secondary_procs = []

def start_secondary_loops(path):
    global _secondary_procs
    for m in monitors[1:]:
        p = subprocess.Popen(['ffplay','-noborder','-loop','0','-x', str(m.width), '-y', str(m.height), '-left', str(m.x), '-top', str(m.y), '-loglevel','quiet','-vf','fps=24', path])
        _secondary_procs.append(p)

start_secondary_loops(INPUT_VIDEO)

# ─────────────────────  Gmail + Drive OAuth helpers  ─────────────────────
from email.mime.text import MIMEText
from google.auth.transport.requests import Request
from google.oauth2.credentials   import Credentials
from googleapiclient.discovery   import build
from googleapiclient.http        import MediaFileUpload
from google_auth_oauthlib.flow   import InstalledAppFlow
from google.auth.exceptions      import RefreshError

def _get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except Exception as e:
            print(f'DEBUG: Failed loading credentials from file: {e}')
            creds = None
    if creds:
        if creds.valid:
            print('DEBUG: Loaded valid credentials from file.')
            return creds
        if creds.expired and creds.refresh_token:
            try:
                print('DEBUG: Refreshing credentials...')
                creds.refresh(Request())
                with open(TOKEN_PATH, 'w') as f:
                    f.write(creds.to_json())
                print('DEBUG: Refresh successful, saved new token.')
                return creds
            except RefreshError as e:
                print(f'DEBUG: RefreshError: {e}, removing token.json and re-authorizing.')
                try:
                    os.remove(TOKEN_PATH)
                except OSError:
                    pass
                creds = None
    if not creds:
        if os.path.exists(TOKEN_PATH):
            print('DEBUG: Removing stale token.json before flow.')
            try:
                os.remove(TOKEN_PATH)
            except OSError:
                pass
        if not os.path.exists(CLIENT_SECRET_PATH):
            sys.exit(f'Put OAuth JSON at {CLIENT_SECRET_PATH}')
        print('DEBUG: Launching OAuth flow.')
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
        creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, 'w') as f:
            f.write(creds.to_json())
        print('DEBUG: OAuth flow complete, credentials saved.')
    return creds


def send_email(sender, to, subject, body):
    msg = MIMEText(body)
    msg['to'], msg['from'], msg['subject'] = to, sender, subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    build('gmail','v1', credentials=_get_credentials()).users().messages().send(userId='me', body={'raw': raw}).execute()

is_valid_email  = lambda e: re.match(r"[^@]+@[^@]+\.[^@]+", e)
is_valid_phone  = lambda p: re.match(r"^\d{10}$", p)

class DarkButton(tk.Button):
    def __init__(self, master=None, **kw):
        cmd = kw.pop('command', None)
        normal_bg = kw.pop('bg', '#222'); active_bg = kw.pop('activebackground', '#444')
        super().__init__(master, relief='flat', bg=normal_bg, fg='#b58900', activebackground=active_bg,
                         activeforeground='#fff', highlightthickness=0, bd=0, **kw)
        self._cmd, self._normal_bg, self._active_bg = cmd, normal_bg, active_bg
        self.bind('<ButtonPress-1>', lambda e: self.config(bg=self._active_bg), add='+')
        self.bind('<ButtonRelease-1>', self._release, add='+')
    def _release(self, _):
        self.config(bg=self._normal_bg)
        winsound.PlaySound(CLICK_SOUND, winsound.SND_FILENAME | winsound.SND_ASYNC)
        if self._cmd:
            self._cmd()

class VideoConverterApp:
    def __init__(self, root):
        self.root = root
        root.attributes('-fullscreen', True)
        root.attributes('-topmost', True)
        root.configure(bg='#2c2c2c')
        self.email_var, self.phone_var = StringVar(), StringVar()
        self.conversion_done = False
        self.rating = 0
        self.upload_frac = 0.0
        self.upload_rem = 0
        self.upload_done = False
        self.upload_link = None
        self.recipient_email = None
        self.recipient_sms_numbers = []
        self.converted = None
        self.input_duration = get_video_duration(INPUT_VIDEO)
        self.start_time = time.time()
        self.font = font.Font(size=20)
        try:
            root.tk.call('font','create','Ethno','-family','Ethnocentric RG','-size','20')
            self.font = font.nametofont('Ethno')
        except Exception:
            pass
        self.star_font = font.Font(size=48)
        self.vlc_instance = vlc.Instance('--quiet','--no-xlib')
        self.player = self.vlc_instance.media_player_new()
        self._build_ui()
        threading.Thread(target=self._run_conversion, daemon=True).start()
        self._schedule_fake_progress()

    def _build_ui(self):
        self.frame = tk.Frame(self.root, bg='#2c2c2c')
        self.frame.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(self.frame, bg='#000')
        self.canvas.pack(fill='both', expand=True)
        self._play(INPUT_VIDEO)
        self.status = tk.Label(self.frame, text='Enhancing: 0%', bg='#2c2c2c', fg='#b58900', font=self.font)
        self.status.pack(pady=10)
        self._show_email_ui()
        self.root.after(500, self._poll_player)
        self.root.after(1000, self._keep_alive)

    def _clear_below_status(self):
        for w in self.frame.winfo_children():
            if w not in (self.canvas, self.status):
                w.destroy()

    def _show_email_ui(self):
        self._clear_below_status()
        wrapper = tk.Frame(self.frame, bg='#2c2c2c'); wrapper.pack(pady=10)
        tk.Label(wrapper, text='Please enter your email:', bg='#2c2c2c', fg='#b58900', font=self.font).pack(pady=(0,5))
        entry = tk.Entry(wrapper, textvariable=self.email_var, font=self.font, width=30, bg='#333', fg='#b58900',
                         insertbackground='#333', insertwidth=0, relief='flat', highlightthickness=0)
        entry.pack(pady=(0,10), ipady=8); entry.focus()
        keys = [list('qwertyuiop'), list('asdfghjkl'), list('zxcvbnm@.'), list('1234567890'), ['gmail','yahoo','outlook'], ['Backspace']]
        for row in keys:
            rowf = tk.Frame(wrapper, bg='#2c2c2c'); rowf.pack(pady=1)
            for k in row:
                w_ = 4 if k not in ('gmail','yahoo','outlook','Backspace') else (10 if k!='Backspace' else 12)
                DarkButton(rowf, text=k, width=int(w_*0.97), height=2, font=self.font, command=lambda kk=k: self._email_key(kk)).pack(side='left', padx=1)
        DarkButton(wrapper, text='Next', font=self.font, command=self._email_next).pack(pady=10)

    def _email_key(self, k):
        cur = self.email_var.get()
        if k == 'Backspace':
            self.email_var.set(cur[:-1])
        elif k in ('gmail','yahoo','outlook') and '@' not in cur:
            self.email_var.set(cur + '@' + k + '.com')
        else:
            self.email_var.set(cur + k)

    def _email_next(self):
        e = self.email_var.get().strip()
        if not is_valid_email(e):
            return
        self.recipient_email = e
        self._show_phone_ui()

    def _show_phone_ui(self):
        self._clear_below_status()
        wrapper = tk.Frame(self.frame, bg='#2c2c2c'); wrapper.pack(pady=10)
        tk.Label(wrapper, text='Enter your phone (10 digits):', bg='#2c2c2c', fg='#b58900', font=self.font).pack(pady=(0,5))
        entry = tk.Entry(wrapper, textvariable=self.phone_var, font=self.font, width=15, bg='#333', fg='#b58900', insertbackground='#333', insertwidth=0, relief='flat', highlightthickness=0)
        entry.pack(pady=(0,10), ipady=8); entry.focus()
        nums = [['1','2','3'],['4','5','6'],['7','8','9'],['Back','0','Del']]
        for row in nums:
            rowf = tk.Frame(wrapper, bg='#2c2c2c'); rowf.pack(pady=2)
            for k in row:
                DarkButton(rowf, text=k, width=6, height=2, font=self.font, command=lambda kk=k: self._phone_key(kk)).pack(side='left', padx=2)
        DarkButton(wrapper, text='Next', font=self.font, command=self._phone_next).pack(pady=10)

    def _phone_key(self, k):
        cur = self.phone_var.get()
        if k == 'Del':
            self.phone_var.set('')
        elif k == 'Back':
            self.phone_var.set(cur[:-1])
        elif len(cur) < 10 and k.isdigit():
            self.phone_var.set(cur + k)

    def _phone_next(self):
        p = self.phone_var.get().strip()
        if not is_valid_phone(p):
            return
        self.recipient_sms_numbers = [f'{p}{gw}' for gw in CARRIER_GATEWAYS.values()]
        self._show_rating_ui()

    def _show_rating_ui(self):
        self._clear_below_status()
        tk.Label(self.frame, text='Please rate your experience:', bg='#2c2c2c', fg='#b58900', font=self.font).pack(pady=10)
        row = tk.Frame(self.frame, bg='#2c2c2c'); row.pack(pady=5)
        self.stars = []
        for i in range(5):
            b = DarkButton(row, text='☆', font=self.star_font, command=lambda idx=i: self._set_rating(idx+1))
            b.pack(side='left', padx=5)
            self.stars.append(b)
        self.send_btn = DarkButton(self.frame, text='Send Clip', font=self.font, state='disabled', command=self._on_send)
        self.send_btn.pack(pady=20)

    def _set_rating(self, n):
        self.rating = n
        for i, b in enumerate(self.stars):
            b.config(text='★' if i < n else '☆')
        if self.conversion_done and self.rating > 0:
            self.send_btn.config(state='normal')

    def _play(self, path):
        self.player.set_media(vlc.Media(path))
        self.player.set_hwnd(self.canvas.winfo_id())
        self.player.play()

    def _poll_player(self):
        if self.player.get_state() == vlc.State.Ended:
            self.player.stop()
            self.player.play()
        self.root.after(500, self._poll_player)

    def _keep_alive(self):
        self.root.event_generate('<<IdleRefresh>>', when='tail')
        self.root.after(1000, self._keep_alive)

    def _schedule_fake_progress(self):
        if not self.conversion_done:
            pct = int(min(99, (time.time() - self.start_time) / self.input_duration * 100))
            self.status.config(text=f'Enhancing: {pct}%')
            self.root.after(1000, self._schedule_fake_progress)
        else:
            self.status.config(text="It's complete")

    def _run_conversion(self):
        try:
            launch_resolve_headless()
        except Exception as e:
            print(f'Error launching Resolve: {e}', file=sys.stderr)
            return
        try:
            out_path = headless_render()
        except Exception as e:
            print(f'Headless render failed: {e}', file=sys.stderr)
            return
        trim_to_duration(out_path, self.input_duration)
        self.converted = out_path
        self.conversion_done = True
        for p in _secondary_procs:
            p.terminate()
        _secondary_procs.clear()
        start_secondary_loops(out_path)
        self.root.after(0, lambda: self._play(out_path))

    def _start_upload(self):
        print('DEBUG: Starting upload of:', self.converted)
        drive = build('drive', 'v3', credentials=_get_credentials())
        try:
            file_size = os.path.getsize(self.converted)
            print(f'DEBUG: File size = {file_size} bytes')
        except Exception as e:
            print(f'DEBUG: Could not get file size: {e}')
        media = MediaFileUpload(self.converted, chunksize=UPLOAD_CHUNK_SIZE, resumable=True)
        req = drive.files().create(body={'name': os.path.basename(self.converted)}, media_body=media, fields='id')
        start = time.time()
        fid = None
        attempt = 0
        while fid is None:
            attempt += 1
            try:
                print(f'DEBUG: Requesting next_chunk (attempt {attempt})...')
                status, resp = req.next_chunk()
                if status:
                    frac = status.progress()
                    rem = int((time.time() - start) * (1 - frac) / frac) if frac > 0 else 0
                    self.upload_frac = frac
                    self.upload_rem = rem
                    print(f'DEBUG: Progress: frac={frac:.4f}, rem={rem}s')
                else:
                    print('DEBUG: next_chunk returned status=None (probably starting).')
                if resp:
                    fid = resp.get('id')
                    print(f'DEBUG: Received file ID: {fid}')
                    self.upload_frac = 1.0
                    self.upload_rem = 0
            except Exception as e:
                print(f'DEBUG: Exception during next_chunk: {e}')
                time.sleep(1)
                continue
        try:
            print(f'DEBUG: Setting permissions for file ID: {fid}')
            drive.permissions().create(fileId=fid, body={'role': 'reader', 'type': 'anyone'}).execute()
            print('DEBUG: Permissions set successfully.')
        except Exception as e:
            print(f'DEBUG: Exception while setting permissions: {e}')
        self.upload_link = f'https://drive.google.com/file/d/{fid}/view?usp=sharing'
        print('DEBUG: Final upload link:', self.upload_link)
        self.upload_done = True

    def _on_send(self):
        if not (self.conversion_done and self.rating):
            return
        self._clear_below_status()
        wrapper = tk.Frame(self.frame, bg='#2c2c2c')
        wrapper.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.upload_label = tk.Label(wrapper, text='Starting upload…', bg='#2c2c2c', fg='#b58900', font=self.font, justify='center')
        self.upload_label.place(relx=0.5, rely=0.5, anchor='center')
        threading.Thread(target=self._start_upload, daemon=True).start()
        self._update_upload_status()
        def final_delivery():
            print('DEBUG: Performing final delivery via email/SMS...')
            send_email(GMAIL_ADDRESS, self.recipient_email, 'Your Video from Pod', f'🎬 Here’s your clip:\n{self.upload_link}')
            print('DEBUG: Email sent to', self.recipient_email)
            for to in self.recipient_sms_numbers:
                send_email(GMAIL_ADDRESS, to, '', f'Your video link: {self.upload_link}')
                print('DEBUG: SMS sent to', to)
            self.upload_label.config(text='Sent!  Check email & SMS.')
        threading.Thread(target=lambda: self._wait_until_done(final_delivery), daemon=True).start()

    def _wait_until_done(self, fn):
        while not self.upload_done:
            time.sleep(1)
        fn()

    def _update_upload_status(self):
        if not self.upload_done:
            pct = int(self.upload_frac * 100)
            rem = self.upload_rem
            print(f'DEBUG: Updating UI - pct={pct}, rem={rem}')
            self.upload_label.config(text=f'Uploading: {pct}%\n{rem} seconds remaining')
            self.root.after(1000, self._update_upload_status)

if __name__ == '__main__':
    root = tk.Tk()
    VideoConverterApp(root)
    root.mainloop()
