#!/usr/bin/env python3
import os
import sys
import socket
import subprocess
import threading
import base64
import re
import time
import types
from shutil import which

import tkinter as tk
from tkinter import font, StringVar

# winsound stub (real on Windows)
try:
    import winsound
except ImportError:
    winsound = types.SimpleNamespace(PlaySound=lambda *args, **kwargs: None)

# screeninfo stub
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

# Resolve SDK paths
resolve_paths = [
    (
        r"C:\Program Files\Blackmagic Design\DaVinci Resolve"
        r"\Support\Developer\Scripting\Modules"
    ),
    (
        r"C:\ProgramData\Blackmagic Design\DaVinci Resolve"
        r"\Support\Developer\Scripting\Modules"
    ),
]
for p in resolve_paths:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

# Constants
RESOLVE_EXE        = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"
SAVED_DIR          = r"C:\Users\agoll\Desktop\VPSRTfr\Saved"
INPUT_VIDEO        = os.path.join(SAVED_DIR, "test_vide.mp4")
PROJECT_PATH       = r"C:\Users\agoll\Desktop\TOOLS\EnhanceTemplate\EnhanceTemplate.drp"
PROJECT_NAME       = "EnhanceTemplate"
TOKEN_PATH         = os.path.join(os.getcwd(), "token.json")
CLIENT_SECRET_PATH = os.path.join(os.getcwd(), "credentials.json")
SCOPES             = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/gmail.send"
]
GMAIL_ADDRESS      = "agollnick1@gmail.com"
CLICK_SOUND        = r"C:\Users\agoll\Desktop\TOOLS\click.wav"

# Email-to-SMS gateways (we'll send to all for redundancy)
CARRIER_GATEWAYS = {
    "ATT": "@txt.att.net",
    "Verizon": "@vtext.com",
    "TMobile": "@tmomail.net",
    "Sprint": "@messaging.sprint.com"
}

# --- Utility helpers ---
def get_video_duration(path):
    """Return video duration in seconds using ffprobe."""
    if not which("ffprobe"):
        return 60.0
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path
        ], text=True)
        return float(out.strip())
    except Exception:
        return 60.0

def trim_to_duration(path, duration):
    """Ensure the exported video matches the desired duration."""
    if not which("ffprobe") or not which("ffmpeg"):
        return
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path
        ], text=True)
        cur = float(out.strip())
        if abs(cur - duration) > 0.1:
            tmp = path + ".trim.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-i", path, "-t", str(duration),
                "-c", "copy", tmp
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            os.replace(tmp, path)
    except Exception:
        pass

def launch_resolve():
    """Start DaVinci Resolve to reduce attach latency."""
    if os.path.isfile(RESOLVE_EXE):
        subprocess.Popen([RESOLVE_EXE], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)

def fullscreen_resolve_window():
    try:
        import pyautogui
        time.sleep(2)
        wins = pyautogui.getWindowsWithTitle("DaVinci Resolve")
        if wins:
            w = wins[0]
            w.maximize()
            pyautogui.hotkey('ctrl','f')
    except:
        pass

# Prevent double-launch
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("localhost", 65432))
except OSError:
    sys.exit("Already running.")

# VLC preview on other monitors
import vlc
monitors = get_monitors()
main_mon = monitors[0]
os.environ["SDL_VIDEO_WINDOW_POS"] = f"{main_mon.x},{main_mon.y}"

secondary_procs = []
def start_secondary_loops(path):
    global secondary_procs
    for m in monitors[1:]:
        p = subprocess.Popen([
            "ffplay","-noborder","-loop","0",
            "-x", str(m.width), "-y", str(m.height),
            "-left", str(m.x), "-top", str(m.y),
            "-loglevel","quiet","-vf","fps=24",
            path
        ])
        secondary_procs.append(p)
start_secondary_loops(INPUT_VIDEO)

# Gmail & Drive helpers
from email.mime.text import MIMEText
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow

def get_credentials():
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            return creds
    if os.path.exists(TOKEN_PATH):
        os.remove(TOKEN_PATH)
    if not os.path.exists(CLIENT_SECRET_PATH):
        sys.exit(f"Put OAuth JSON at {CLIENT_SECRET_PATH}")
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    return creds

def send_email(sender, to, subject, body):
    msg = MIMEText(body)
    msg["to"], msg["from"], msg["subject"] = to, sender, subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    svc = build("gmail","v1",credentials=get_credentials())
    svc.users().messages().send(userId="me", body={"raw": raw}).execute()

def is_valid_email(e): return re.match(r"[^@]+@[^@]+\.[^@]+", e)
def is_valid_phone(p): return re.match(r"^\d{10}$", p)

class DarkButton(tk.Button):
    """Custom button with better touch feedback."""

    def __init__(self, master=None, **kw):
        self._cmd = kw.pop("command", None)
        normal_bg = kw.pop("bg", "#222222")
        active_bg = kw.pop("activebackground", "#444444")
        super().__init__(
            master,
            relief="flat",
            bg=normal_bg,
            fg="#b58900",
            activebackground=active_bg,
            activeforeground="#ffffff",
            highlightthickness=0,
            bd=0,
            **kw,
        )
        self._normal_bg = normal_bg
        self._active_bg = active_bg
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")

    def _on_press(self, event):
        self.configure(bg=self._active_bg)

    def _on_release(self, event):
        self.configure(bg=self._normal_bg)
        winsound.PlaySound(CLICK_SOUND, winsound.SND_FILENAME | winsound.SND_ASYNC)
        if self._cmd:
            self._cmd()

class VideoConverterApp:
    def __init__(self, root):
        self.root            = root
        root.attributes("-fullscreen", True)
        root.attributes("-topmost", True)
        root.configure(bg="#2c2c2c")

        # State
        self.email_var    = StringVar()
        self.phone_var    = StringVar()
        self.fake_pct     = 0
        self.conversion_done = False
        self.phase        = "email"   # email -> phone -> rating -> upload
        self.rating       = 0
        self.upload_frac  = 0.0
        self.upload_rem   = 0
        self.upload_done  = False
        self.upload_link  = None
        self.recipient_email = None
        self.recipient_sms_numbers = []
        self.converted    = None

        # Fonts
        self.font      = font.Font(size=20)
        try:
            root.tk.call("font","create","Ethno","-family","Ethnocentric RG","-size","20")
            self.font = tk.font.nametofont("Ethno")
        except: pass
        self.star_font = font.Font(size=48)

        # Video info
        self.input_duration = get_video_duration(INPUT_VIDEO)
        self.start_time = time.time()

        # Launch Resolve early for faster attachment
        threading.Thread(target=launch_resolve, daemon=True).start()

        # VLC
        self.vlc_mod      = vlc
        self.vlc_instance = vlc.Instance("--quiet","--no-xlib")
        self.player       = self.vlc_instance.media_player_new()

        self.build_ui()
        threading.Thread(target=self.run_conversion, daemon=True).start()
        threading.Thread(target=self.fake_progress, daemon=True).start()

    def build_ui(self):
        self.frame = tk.Frame(self.root, bg="#2c2c2c"); self.frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(self.frame, bg="#000"); self.canvas.pack(fill="both", expand=True)
        self.play(INPUT_VIDEO)
        self.status = tk.Label(self.frame, text="Enhancing: 0%", bg="#2c2c2c", fg="#b58900", font=self.font)
        self.status.pack(pady=10)
        self.show_email_ui()
        self.root.after(500, self.poll)
        self.root.after(1000, self.keep_alive)

    def clear_ui(self):
        for w in self.frame.winfo_children():
            if w not in (self.canvas, self.status):
                w.destroy()

    # — EMAIL STEP with touch keyboard —
    def show_email_ui(self):
        self.clear_ui(); self.phase="email"
        ef = tk.Frame(self.frame, bg="#2c2c2c"); ef.pack(pady=10)
        tk.Label(ef, text="Please enter your email:", bg="#2c2c2c", fg="#b58900", font=self.font).pack(pady=(0,5))
        e = tk.Entry(
            ef,
            textvariable=self.email_var,
            font=self.font,
            width=30,
            bg="#333333",
            fg="#b58900",
            insertbackground="#333333",
            insertwidth=0,
            relief="flat",
            highlightthickness=0,
        )
        e.pack(pady=(0,10), ipady=8); e.focus()

        keys = [
            list("qwertyuiop"), list("asdfghjkl"),
            list("zxcvbnm@."), list("1234567890"),
            ["gmail","yahoo","outlook"], ["Backspace"]
        ]
        for row in keys:
            rf = tk.Frame(ef, bg="#2c2c2c"); rf.pack(pady=1)
            for k in row:
                w = 4 if k not in ["gmail","yahoo","outlook","Backspace"] else (10 if k!="Backspace" else 12)
                DarkButton(rf, text=k, width=int(w*0.97), height=2,
                           font=self.font, command=lambda kk=k: self.on_email_key(kk)).pack(side="left", padx=1)

        DarkButton(ef, text="Next", font=self.font, command=self.on_email_next).pack(pady=10)

    def on_email_key(self, k):
        cur = self.email_var.get()
        if k=="Backspace":
            self.email_var.set(cur[:-1])
        elif k in ["gmail","yahoo","outlook"] and "@" not in cur:
            self.email_var.set(cur + "@" + k + ".com")
        else:
            self.email_var.set(cur + k)

    def on_email_next(self):
        email = self.email_var.get().strip()
        if not is_valid_email(email): return
        self.recipient_email = email
        self.show_phone_ui()

    # — PHONE STEP (no carrier select) —
    def show_phone_ui(self):
        self.clear_ui(); self.phase="phone"
        pf = tk.Frame(self.frame, bg="#2c2c2c"); pf.pack(pady=10)
        tk.Label(pf, text="Enter your phone (10 digits):", bg="#2c2c2c", fg="#b58900", font=self.font).pack(pady=(0,5))
        pe = tk.Entry(
            pf,
            textvariable=self.phone_var,
            font=self.font,
            width=15,
            bg="#333333",
            fg="#b58900",
            insertbackground="#333333",
            insertwidth=0,
            relief="flat",
            highlightthickness=0,
        )
        pe.pack(pady=(0,10), ipady=8); pe.focus()

        nums = [["1","2","3"],["4","5","6"],["7","8","9"],["Back","0","Del"]]
        for row in nums:
            rf = tk.Frame(pf, bg="#2c2c2c"); rf.pack(pady=2)
            for k in row:
                DarkButton(rf, text=k, width=6, height=2, font=self.font,
                           command=lambda kk=k: self.on_phone_key(kk)).pack(side="left", padx=2)

        DarkButton(pf, text="Next", font=self.font, command=self.on_phone_next).pack(pady=10)

    def on_phone_key(self, k):
        cur = self.phone_var.get()
        if k=="Del":
            self.phone_var.set("")
        elif k=="Back":
            self.phone_var.set(cur[:-1])
        elif len(cur)<10 and k.isdigit():
            self.phone_var.set(cur+k)

    def on_phone_next(self):
        phone = self.phone_var.get().strip()
        if not is_valid_phone(phone): return
        # prepare SMS recipients for all gateways
        self.recipient_sms_numbers = [f"{phone}{gw}" for gw in CARRIER_GATEWAYS.values()]
        self.show_rating_ui()

    # — RATING STEP —
    def show_rating_ui(self):
        self.clear_ui(); self.phase="rating"
        tk.Label(self.frame, text="Please rate your experience:", bg="#2c2c2c", fg="#b58900", font=self.font).pack(pady=10)
        rf = tk.Frame(self.frame, bg="#2c2c2c"); rf.pack(pady=5)
        self.stars = []
        for i in range(5):
            b = DarkButton(rf, text="☆", font=self.star_font,
                           command=lambda idx=i: self.set_rating(idx+1))
            b.pack(side="left", padx=5)
            self.stars.append(b)
        self.next_btn = DarkButton(self.frame, text="Send Clip", font=self.font, state="disabled", command=self.on_send)
        self.next_btn.pack(pady=20)

    def set_rating(self, n):
        self.rating = n
        for i,b in enumerate(self.stars):
            b.config(text="★" if i < n else "☆")
        if self.conversion_done and self.rating>0:
            self.next_btn.config(state="normal")

    # — PLAY, POLL, KEEPALIVE, FAKE PROGRESS —
    def play(self, path):
        m = self.vlc_mod.Media(path)
        self.player.set_media(m)
        self.player.set_hwnd(self.canvas.winfo_id())
        self.player.play()

    def poll(self):
        if self.player.get_state() == self.vlc_mod.State.Ended:
            self.player.stop()
            self.player.play()
        self.root.after(500, self.poll)

    def keep_alive(self):
        self.root.event_generate("<<IdleRefresh>>", when="tail")
        self.root.after(1000, self.keep_alive)

    def fake_progress(self):
        """Update progress based on elapsed time."""
        while not self.conversion_done:
            elapsed = time.time() - self.start_time
            pct = int(min(99, (elapsed / self.input_duration) * 100))
            self.root.after(0, lambda p=pct: self.status.config(text=f"Enhancing: {p}%"))
            time.sleep(1)
        self.root.after(0, lambda: self.status.config(text="It's complete"))

    # — CONVERSION —
    def run_conversion(self):
        try:
            import DaVinciResolveScript as dvr
        except ImportError:
            print("SDK not found", file=sys.stderr)
            return

        if not os.path.isfile(RESOLVE_EXE):
            print(f"Resolve.exe missing at {RESOLVE_EXE}", file=sys.stderr)
            return

        resolve = dvr.scriptapp("Resolve")
        attempts = 0
        if not resolve:
            launch_resolve()
        while not resolve and attempts < 10:
            time.sleep(2)
            resolve = dvr.scriptapp("Resolve")
            attempts += 1
        if not resolve:
            print(f"Can't attach after {attempts} attempts", file=sys.stderr)
            return

        pm = resolve.GetProjectManager()
        if not pm.LoadProject(PROJECT_NAME):
            if os.path.exists(PROJECT_PATH):
                pm.ImportProject(PROJECT_PATH)
                if not pm.LoadProject(PROJECT_NAME):
                    print("Failed to load template", file=sys.stderr)
                    return
            else:
                print("Project file missing", file=sys.stderr)
                return
        proj = pm.GetCurrentProject()
        tl   = proj.GetCurrentTimeline() or proj.GetTimelineByIndex(1)

        proj.SetSetting("timelineResolutionWidth","2160")
        proj.SetSetting("timelineResolutionHeight","3840")
        proj.SetSetting("timelineOutputResolutionWidth","2160")
        proj.SetSetting("timelineOutputResolutionHeight","3840")

        base = os.path.splitext(os.path.basename(INPUT_VIDEO))[0] + "_enhanced"
        rs = {
            "TargetDir": SAVED_DIR,
            "CustomName": base,
            "Format": "mp4",
            "VideoCodec": "H.265",
            "Encoder": "NVIDIA",
            "ExportVideo": True,
            "ExportAudio": True,
            "Quality": "Best",
            "RateControl": "VBR",
            "SplitMode": "Split",
            "Preset": "Fast",
            "Tuning": "High Quality",
            "TwoPass": False,
            "Lookahead": "16",
            "LookaheadLevel": "0",
            "EnableAdaptiveBFrame": True,
            "AQStrength": "8",
            "PixelAspectRatio": "Square",
            "DataLevels": "Auto",
            "ColorSpaceTag": "SameAsProject",
            "GammaTag": "SameAsProject",
            "BypassReEncodeWhenPossible": True
        }
        proj.SetRenderSettings(rs)
        proj.DeleteAllRenderJobs()
        job_id = proj.AddRenderJob()
        self.start_time = time.time()
        proj.StartRendering(job_id)
        threading.Thread(target=fullscreen_resolve_window, daemon=True).start()

        while proj.IsRenderingInProgress():
            time.sleep(0.5)

        out = os.path.join(SAVED_DIR, base + ".mp4")
        trim_to_duration(out, self.input_duration)
        self.converted = out
        self.conversion_done = True

        for p in secondary_procs:
            p.terminate()
        secondary_procs.clear()
        start_secondary_loops(out)
        self.root.after(0, lambda: self.play(out))

    # — UPLOAD & SEND —
    def start_upload(self):
        drive = build("drive","v3", credentials=get_credentials())
        media = MediaFileUpload(self.converted, chunksize=1024*1024, resumable=True)
        req   = drive.files().create(
            body={"name": os.path.basename(self.converted)},
            media_body=media, fields="id"
        )
        start, fid = time.time(), None
        while fid is None:
            status, resp = req.next_chunk()
            if status:
                frac = status.progress()
                rem  = int((time.time()-start)*(1-frac)/frac) if frac>0 else 0
                self.upload_frac, self.upload_rem = frac, rem
            if resp:
                fid = resp.get("id")
        drive.permissions().create(fileId=fid, body={"role":"reader","type":"anyone"}).execute()
        self.upload_link, self.upload_done = f"https://drive.google.com/file/d/{fid}/view?usp=sharing", True

    def on_send(self):
        if not self.conversion_done or self.rating == 0:
            return
        self.clear_ui()
        uf = tk.Frame(self.frame, bg="#2c2c2c"); uf.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.upload_label = tk.Label(uf, text="Starting upload...", bg="#2c2c2c",
                                     fg="#b58900", font=self.font, justify="center")
        self.upload_label.place(relx=0.5, rely=0.5, anchor="center")
        threading.Thread(target=self.start_upload, daemon=True).start()
        self.update_status()

        def final_send():
            # send email
            send_email(GMAIL_ADDRESS, self.recipient_email,
                       "Your Video from Pod", f"🎬 Your video is here:\n{self.upload_link}")
            # send SMS to every gateway
            for to in self.recipient_sms_numbers:
                send_email(GMAIL_ADDRESS, to, "", f"Your video link: {self.upload_link}")

        threading.Thread(target=lambda: (self.wait_and(final_send)), daemon=True).start()

    def wait_and(self, fn):
        while not self.upload_done:
            time.sleep(1)
        fn()
        self.upload_label.config(text="Sent! Check email/SMS.")

    def update_status(self):
        if not self.upload_done:
            pct = int(self.upload_frac * 100)
            rem = self.upload_rem
            self.upload_label.config(text=f"Uploading: {pct}%\n{rem} seconds remaining")
            self.root.after(1000, self.update_status)

if __name__ == "__main__":
    root = tk.Tk()
    VideoConverterApp(root)
    root.mainloop()
