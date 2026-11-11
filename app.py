"""TruyenSaver Coc Coc Offline v1.0

Luu trang qua chinh hop thoai Ctrl+S cua Coc Coc (HTML + thu muc tai nguyen),
khong tu tai anh tu HTTP va khong tu ghi lai HTML. Windows 10/11 only.
"""
from __future__ import annotations

import html
import math
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urljoin

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

APP_NAME = "TruyenSaver Coc Coc Offline"
VERSION = "1.1.0"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "TruyenSaverCocCoc"
PROFILE_DIR = CONFIG_DIR / "CocCoc_Profile_Offline"
CONFIG_FILE = CONFIG_DIR / "settings.json"
SUPPORTED_DOMAINS = ("damconuong.cool", "sayhentai.cx")
CHAPTER_RE = re.compile(r"(?:chap(?:ter)?|chương|chuong)\s*[:#\-_. ]*\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)


def sanitize(text: str, fallback: str = "Truyen", limit: int = 90) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(text)).strip(" .")
    return (text[:limit].rstrip(" .") or fallback)


def norm_num(num: str) -> str:
    x = float(num.replace(",", "."))
    return f"{int(x):03d}" if x.is_integer() else (f"{x:.2f}".rstrip("0").rstrip("."))


def chapter_from_anchor(text: str, link: str, series_url: str) -> float | None:
    """Only accept links clearly tied to the given series, not sidebar recommendations."""
    parent_path = urlparse(series_url).path.rstrip("/").lower()
    parsed = urlparse(link)
    path = parsed.path.lower().rstrip("/")
    same_host = (parsed.hostname or "").removeprefix("www.") == (urlparse(series_url).hostname or "").removeprefix("www.")
    if not same_host:
        return None
    series_slug = parent_path.rsplit("/", 1)[-1].replace(".html", "")
    host = (parsed.hostname or "").lower()
    if "damconuong" in host:
        if not path.startswith(parent_path + "/"):
            return None
    elif "sayhentai" in host:
        # The site can use /truyen-name-chap-N.html or /truyen-name/chap-N.
        if not (path.startswith(parent_path + "/") or path.startswith(parent_path.removesuffix(".html") + "-") or (series_slug in path and path != parent_path)):
            return None
    else:
        if not path.startswith(parent_path + "/"):
            return None
    matches = CHAPTER_RE.findall(text + " " + path.replace("-", " ").replace("_", " "))
    if not matches:
        return None
    try:
        return float(matches[-1].replace(",", "."))
    except ValueError:
        return None


def extract_chapters(anchors: list[dict], series_url: str) -> list[dict]:
    out = {}
    for a in anchors:
        url = urljoin(series_url, a.get("href") or "")
        number = chapter_from_anchor(a.get("text", ""), url, series_url)
        if number is None:
            continue
        if number not in out:
            out[number] = {"number": number, "url": url}
    return [out[k] for k in sorted(out)]


def find_coccoc() -> str | None:
    base = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "CocCoc/Browser/Application/browser.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "CocCoc/Browser/Application/browser.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "CocCoc/Browser/Application/browser.exe",
    ]
    for path in base:
        if path.is_file():
            return str(path)
    return None


def browser_ready(port: int) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.75) as res:
            return res.status == 200
    except Exception:
        return False


def random_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class BrowserHandle:
    path: str
    proc: subprocess.Popen | None = None
    port: int | None = None

    def ensure(self) -> int:
        if self.port and browser_ready(self.port):
            return self.port
        self.port = random_port()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen([
            self.path,
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if browser_ready(self.port):
                return self.port
            if self.proc.poll() is not None:
                raise RuntimeError("Cốc Cốc đóng ngay sau khi mở. Kiểm tra đường dẫn browser.exe.")
            time.sleep(0.3)
        raise RuntimeError("Cốc Cốc không bật cổng điều khiển. Hãy đóng cửa sổ Cốc Cốc Offline và thử lại.")


# Inspect only likely reader elements; never treat a loaded banner as proof of comic completeness.
READER_SCAN_JS = r"""() => {
 const sels = [
  '.reading-detail','.chapter-content','.page-chapter','.viewer-content',
  '.reading-content','#chapter-content','#chapter-reading-content',
  '.container-chapter-reader','.chapter-images','.reading__content',
  '.wp-manga-chapter-img','#reader','.entry-content','.page-break',
  '.reading','.chap-content','#chapter_content','.chapter-reader',
  '.reading-area','.chapter_pages','#image-container',
  '.chapter-content-wrap','.chapter-canvas','.chapter-photos',
  '#readerarea','.viewer','#chapter-body','#chapter-image',
  '.read-content','.read-container','.chapter-images-container'
 ];
 let choices=[];
 for (const [rank,sel] of sels.entries()) {
  for (const el of document.querySelectorAll(sel)) {
   const n=el.querySelectorAll('img').length;
   if (n>0) choices.push({el,sel,n,rank});
  }
 }
 choices.sort((a,b) => (a.n>=2?0:1)-(b.n>=2?0:1) || a.rank-b.rank || b.n-a.n);
 let root=choices.length ? choices[0].el : null;
 let selector=choices.length ? choices[0].sel : '';
 const imgs=Array.from(root ? root.querySelectorAll('img') : []);
 const details=imgs.map(i => {
  const u=i.currentSrc||i.src||i.getAttribute('data-src')||'';
  const pending=i.getAttribute('data-src')||i.getAttribute('data-original')||i.getAttribute('data-lazy-src')||'';
  const placeholder=(u.startsWith('data:') || /placeholder|loading\.gif|lazy\.gif/i.test(u));
  const ready=Boolean(i.complete && i.naturalWidth>0 && i.naturalHeight>0 && !(pending && placeholder));
  return {url:u, ready, w:i.naturalWidth||0,h:i.naturalHeight||0};
 });
 return {selector,total:details.length,ready:details.filter(x=>x.ready).length,
         missing:details.filter(x=>!x.ready).length,
         valid:details.filter(x=>x.ready && x.w>=120 && x.h>=120).length,
         sample:details.slice(0,3), height:document.documentElement.scrollHeight,
         atBottom:scrollY+innerHeight>=document.documentElement.scrollHeight-12};
}"""


def scroll_until_stable(page, log, stop: threading.Event, max_seconds=190) -> dict:
    """DOM image state is an indication, not a proof every page image exists server-side."""
    started = time.monotonic()
    page.evaluate("window.scrollTo(0,0)")
    time.sleep(0.6)
    prior = None
    stable = 0
    passes = 0
    last = {}
    while time.monotonic() - started < max_seconds:
        if stop.is_set():
            raise InterruptedError("Đã dừng theo yêu cầu")
        info = page.evaluate(READER_SCAN_JS)
        last = info
        if not info["selector"]:
            try:
                text = page.evaluate("document.body.innerText.slice(0,9000)")
                if "Vui lòng đăng nhập để xem nội dung này" in text:
                    raise RuntimeError("Trang yêu cầu đăng nhập. Mở Cốc Cốc Offline, đăng nhập rồi tải lại.")
            except RuntimeError: raise
            except Exception: pass
            # Give scripts time to generate the reading container.
            if time.monotonic() - started > 18:
                raise RuntimeError("Không nhận diện được vùng ảnh truyện. Không lưu nhầm quảng cáo.")
            time.sleep(1.0)
            continue
        if info["atBottom"]:
            passes += 1
            state = (info["total"], info["ready"], info["missing"], info["height"])
            if prior == state and info["total"] > 0 and info["missing"] == 0:
                stable += 1
            else:
                stable = 0
            prior = state
            if stable >= 5 and info["valid"] >= 1:  # ~5 seconds stable after bottom
                log(f"    Đã ổn định: {info['ready']}/{info['total']} ảnh trong {info['selector']}.")
                return info
            if info["missing"] > 0 and passes % 6 == 0:
                log(f"    Còn {info['missing']} ảnh chưa tải; đang chờ/thử cuộn lại...")
            # Re-visit top periodically to trigger virtualized/lazy images.
            if passes % 12 == 0:
                page.evaluate("window.scrollTo(0,0)")
                time.sleep(0.7)
        else:
            passes = 0
            stable = 0
            page.evaluate("window.scrollBy(0, Math.max(450, Math.round(innerHeight*0.82)))")
        time.sleep(0.85)
    raise RuntimeError(
        f"Hết thời gian chờ ảnh ({max_seconds}s): {last.get('ready',0)}/{last.get('total',0)}. "
        "Không lưu vì có thể thiếu ảnh."
    )


def folder_for_html(path: Path) -> Path | None:
    # Do not accept unrelated folders that happen to start with this chapter name.
    for suffix in ("_files", "_tệp", "_files (1)"):
        d = path.with_name(path.stem + suffix)
        if d.is_dir():
            return d
    return None


def looks_like_image(path: Path) -> bool:
    """Check a significant image file by signature, not extension alone."""
    try:
        if path.stat().st_size < 2048:
            return False
        with path.open("rb") as f:
            h = f.read(32)
        return (h.startswith(b"\xff\xd8\xff") or h.startswith(b"\x89PNG\r\n\x1a\n")
                or h.startswith((b"GIF87a", b"GIF89a", b"BM"))
                or (h.startswith(b"RIFF") and h[8:12] == b"WEBP")
                or (h[4:8] == b"ftyp" and (b"avif" in h or b"avis" in h)))
    except (OSError, PermissionError):
        return False


def saved_ok(path: Path, expected_images: int | None = None) -> tuple[bool, int]:
    """Best-effort post-save check, NOT a proof that offline page is complete.

    A logo/icon or an image-looking filename alone must not mark a whole chap
    successful. Require a majority of the reader images present as local files.
    """
    try:
        if not path.is_file() or path.stat().st_size < 1024:
            return False, 0
    except OSError:
        return False, 0
    folder = folder_for_html(path)
    if folder is None:
        return False, 0
    try:
        files = (p for p in folder.rglob("*") if p.is_file())
        count = sum(1 for p in files if looks_like_image(p))
    except OSError:
        return False, 0
    minimum = (max(1, math.ceil(expected_images * 0.8))
               if expected_images is not None and expected_images > 0 else 1)
    return count >= minimum, count


def checkpoint_path(folder: Path) -> Path:
    return folder / "TIEN_DO_LUU.json"


def read_checkpoint(folder: Path) -> dict:
    try:
        data = json.loads(checkpoint_path(folder).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def record_checkpoint(folder: Path, chap: str, expected: int, counted: int, source: str):
    data = read_checkpoint(folder)
    data[chap] = {"expected_images": int(expected), "saved_images": int(counted), "url": source,
                  "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "version": VERSION}
    dst = checkpoint_path(folder)
    tmp = dst.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, dst)


def verified_previous_save(folder: Path, chap: str, html_file: Path, source: str) -> bool:
    entry = read_checkpoint(folder).get(chap)
    if not isinstance(entry, dict) or entry.get("url") != source:
        return False
    try:
        expected = int(entry["expected_images"])
    except (ValueError, TypeError, KeyError):
        return False
    # Old saves, or files without a matching checkpoint, are not blindly skipped.
    return expected > 0 and saved_ok(html_file, expected)[0]


def backup_partial(path: Path):
    if not path.exists() and not folder_for_html(path):
        return
    backup_dir = path.parent / "_Luu_thieu_can_kiem_tra"
    backup_dir.mkdir(exist_ok=True)
    suffix = time.strftime("%Y%m%d_%H%M%S")
    original_folder = folder_for_html(path)
    if path.exists():
        shutil.move(str(path), str(backup_dir / f"{path.stem}_{suffix}{path.suffix}"))
    if original_folder:
        shutil.move(str(original_folder), str(backup_dir / f"{original_folder.name}_{suffix}"))


def set_filetype_complete(dlg) -> bool:
    """Select 'Webpage, Complete' in common Save As dialogs (Vietnamese/English).

    If accessibility APIs cannot reveal the type, fail closed: never silently
    save HTML-only and pretend it is an offline complete page.
    """
    try:
        combos = dlg.descendants(control_type="ComboBox")
    except Exception:
        combos = []

    def is_complete(label):
        txt = str(label).casefold()
        return ("complete" in txt or "hoàn chỉnh" in txt or "đầy đủ" in txt) and ("web" in txt or "trang" in txt)

    for combo in combos:
        try:
            if is_complete(combo.window_text()):
                return True
            options = combo.item_texts()
            for option in options:
                if is_complete(option):
                    combo.select(option)
                    return True
        except Exception:
            continue
    # Some Windows dialogs don't enumerate options until expanded.
    for combo in combos:
        try:
            combo.expand()
            for item in combo.descendants(control_type="ListItem"):
                label = item.window_text()
                if is_complete(label):
                    item.click_input()
                    return True
            combo.collapse()
        except Exception:
            continue
    return False


def perform_ctrl_s_save(path: Path, browser_pid: int | None, browser_exe: str,
                        stop: threading.Event, log, expected_images: int) -> int:
    """Actually press Ctrl+S in Coc Coc and use its native Windows Save As dialog."""
    if sys.platform != "win32":
        raise RuntimeError("Chức năng Ctrl+S chỉ hoạt động trên Windows.")
    from pywinauto import Desktop, Application, keyboard
    import win32gui, win32con, win32process
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_partial(path)
    hwnds = []
    def collect(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd).startswith("Chrome_WidgetWin"):
                hwnds.append((hwnd, pid))
        except Exception:
            pass
    win32gui.EnumWindows(collect, None)
    matching = [x[0] for x in hwnds if browser_pid and x[1] == browser_pid]
    # Launchers can exit while the browser remains active; find the managed
    # Coc Coc profile by process arguments, never focus a random Chrome window.
    if not matching:
        try:
            import psutil
            for hwnd, pid in hwnds:
                try:
                    proc = psutil.Process(pid)
                    cmd = " ".join(proc.cmdline()).casefold()
                    exe = proc.exe().casefold()
                    if exe == str(Path(browser_exe)).casefold() and str(PROFILE_DIR).casefold() in cmd:
                        matching.append(hwnd)
                except (psutil.Error, OSError):
                    continue
        except ImportError:
            pass
    if not matching:
        raise RuntimeError("Không tìm được cửa sổ Cốc Cốc Offline để nhấn Ctrl+S. Đừng đóng cửa sổ trình duyệt.")
    hwnd = matching[0]
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        Application(backend="uia").connect(handle=hwnd).window(handle=hwnd).set_focus()
    except Exception:
        try: win32gui.SetForegroundWindow(hwnd)
        except Exception: pass
    time.sleep(0.5)
    keyboard.send_keys("^s")
    log("    Đã gửi Ctrl+S, đang tìm hộp thoại Lưu trang...")
    dlg = None
    deadline = time.monotonic() + 18
    while time.monotonic() < deadline and not stop.is_set():
        for win in Desktop(backend="uia").windows():
            try:
                title = win.window_text().casefold()
                if ("save as" in title or "save page" in title or "lưu dưới dạng" in title or "lưu trang" in title or "lưu thành" in title) and win.is_visible():
                    dlg = win
                    break
            except Exception:
                continue
        if dlg:
            break
        time.sleep(0.35)
    if not dlg:
        raise RuntimeError("Không thấy hộp thoại Ctrl+S của Cốc Cốc. Kiểm tra trình duyệt và các cửa sổ đang mở.")
    if not set_filetype_complete(dlg):
        keyboard.send_keys("{ESC}")
        raise RuntimeError("Không xác nhận được 'Trang web, hoàn chỉnh'. Không lưu vì có thể thiếu ảnh; cần chỉnh cho hộp thoại Cốc Cốc trên máy bạn.")
    try:
        edits = dlg.descendants(control_type="Edit")
        named = [e for e in edits if e.element_info.automation_id in ("1001", "FileNameControlHost")]
        target = named[0] if named else (edits[-1] if edits else None)
        if target is None:
            raise RuntimeError("Không tìm thấy ô tên file trong hộp thoại lưu.")
        target.set_edit_text(str(path))
        time.sleep(0.25)
        # Pressing Enter in the filename field invokes Save in standard Windows dialogs.
        target.set_focus()
        keyboard.send_keys("{ENTER}")
    except Exception as exc:
        raise RuntimeError(f"Không nhập được đường dẫn lưu: {exc}")
    started = time.monotonic()
    last_sig = None
    stable = 0
    while time.monotonic() - started < 150:
        if stop.is_set():
            raise InterruptedError("Đã dừng (có thể cần đóng hộp thoại lưu thủ công)")
        folder = folder_for_html(path)
        if path.exists() and folder:
            try:
                paths = [p for p in folder.rglob("*") if p.is_file()]
                sig = (path.stat().st_size, len(paths), sum(p.stat().st_size for p in paths))
            except OSError:
                time.sleep(.8)
                continue
            if sig == last_sig:
                stable += 1
            else:
                stable = 0
                last_sig = sig
            if stable >= 4:
                ok, assets = saved_ok(path, expected_images)
                if ok:
                    return assets
        time.sleep(0.9)
    ok, found = saved_ok(path, expected_images)
    raise RuntimeError(f"Cốc Cốc chưa lưu đủ ảnh vào thư mục offline (tìm thấy {found}, "
                       f"cần ít nhất {max(1, math.ceil(expected_images*0.8))}). "
                       "Đã giữ bản lưu để kiểm tra, không đánh dấu hoàn tất.")


def write_index(folder: Path, saved: list[dict], title: str):
    items = []
    for chapter in sorted(saved, key=lambda x: x["number"]):
        number = norm_num(str(chapter["number"]))
        p = folder / f"Chap_{number}.html"
        if saved_ok(p)[0]:
            items.append(f'<li><a href="{html.escape(p.name,quote=True)}">Chap {html.escape(number)}</a></li>')
    markup = ("<!doctype html><html lang='vi'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Mục lục offline</title>"
        "<style>body{font:17px Arial,sans-serif;max-width:720px;margin:40px auto;padding:16px}"
        "li{margin:10px 0}a{color:#1664c0}</style>"
        f"<h1>{html.escape(title)}</h1><p>Mở từng chap không cần Internet (cần kiểm tra offline lần đầu).</p>"
        "<ol>"+"".join(items)+"</ol></html>")
    (folder / "MUC_LUC.html").write_text(markup, encoding="utf-8")


class OfflineSaverApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME + " v" + VERSION)
        self.root.geometry("840x725")
        self.root.minsize(730, 630)
        self.msgs = queue.Queue()
        self.stop_event = threading.Event()
        self.busy = False
        self.config = self.read_config()
        self.browser = BrowserHandle(self.config.get("browser") or find_coccoc() or "", port=self.config.get("port"))
        self.chapters_by_url = {}
        self.start_var = tk.StringVar(value="1")
        self.end_var = tk.StringVar(value="100")
        self.folder_var = tk.StringVar(value=self.config.get("folder") or str(Path.home() / "Downloads" / "Truyen_Offline"))
        self.browser_var = tk.StringVar(value=self.browser.path)
        self.progress_var = tk.StringVar(value="Chưa bắt đầu")
        self.progress = None
        self.build_ui()
        self.url_text.insert("1.0", self.config.get("urls") or "https://damconuong.cool/truyen/ban-hoc")
        self.root.after(140, self.process_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self):
        base = ttk.Frame(self.root, padding=18)
        base.pack(fill="both", expand=True)
        ttk.Label(base, text="TruyenSaver – Cốc Cốc Offline", font=("Segoe UI", 19, "bold")).pack(anchor="w")
        ttk.Label(base, text="Ctrl+S của Cốc Cốc: lưu HTML + thư mục ảnh. Kiểm tra số ảnh trước/sau khi lưu.", foreground="#475569").pack(anchor="w", pady=(3, 14))
        ttk.Label(base, text="Link trang thông tin truyện (mỗi dòng một bộ; có thể xếp hàng 2 website):").pack(anchor="w")
        self.url_text = tk.Text(base, height=3, wrap="word", font=("Segoe UI", 10))
        self.url_text.pack(fill="x", pady=(5, 9))
        ranges = ttk.Frame(base)
        ranges.pack(fill="x", pady=(2, 10))
        ttk.Label(ranges, text="Từ chap").pack(side="left", padx=(0, 6))
        ttk.Entry(ranges, textvariable=self.start_var, width=8).pack(side="left", padx=(0, 20))
        ttk.Label(ranges, text="Đến chap").pack(side="left", padx=(0, 6))
        ttk.Entry(ranges, textvariable=self.end_var, width=8).pack(side="left")
        ttk.Label(base, text="Thư mục lưu trên máy:").pack(anchor="w")
        r = ttk.Frame(base); r.pack(fill="x", pady=(4, 8))
        ttk.Entry(r, textvariable=self.folder_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="Chọn thư mục…", command=self.pick_folder).pack(side="left", padx=(7, 0))
        ttk.Button(r, text="Mở thư mục", command=self.open_folder).pack(side="left", padx=(6, 0))
        ttk.Label(base, text="Cốc Cốc (browser.exe):").pack(anchor="w")
        r2 = ttk.Frame(base); r2.pack(fill="x", pady=(4, 11))
        ttk.Entry(r2, textvariable=self.browser_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r2, text="Tìm file…", command=self.pick_browser).pack(side="left", padx=(7, 0))
        tools = ttk.Frame(base); tools.pack(fill="x", pady=(1, 12))
        self.login_btn = ttk.Button(tools, text="1. Mở Cốc Cốc / đăng nhập", command=self.open_browser)
        self.login_btn.pack(side="left", padx=(0, 8))
        self.scan_btn = ttk.Button(tools, text="2. Quét danh sách chap", command=self.scan)
        self.scan_btn.pack(side="left", padx=(0, 8))
        self.go_btn = ttk.Button(tools, text="3. BẮT ĐẦU LƯU", command=self.start)
        self.go_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(tools, text="Dừng", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Label(base, text="Trạng thái:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(base, textvariable=self.progress_var).pack(anchor="w", pady=(2, 6))
        self.progress = ttk.Progressbar(base, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 10))
        ttk.Label(base, text="Nhật ký:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.logs = scrolledtext.ScrolledText(base, height=13, wrap="word", font=("Consolas", 9), state="disabled")
        self.logs.pack(fill="both", expand=True, pady=(5, 8))
        ttk.Label(base, text="Lưu tuần tự vì Ctrl+S dùng một hộp thoại. Không đóng Cốc Cốc Offline hoặc dùng bàn phím khi đang lưu.", foreground="#9a3412").pack(anchor="w")
        self.append_log("Sẵn sàng. Hãy thử 1–2 chap và mở HTML sau khi ngắt mạng để kiểm tra.")

    def read_config(self):
        try: return json.loads(CONFIG_FILE.read_text(encoding="utf8"))
        except Exception: return {}

    def save_config(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"folder": self.folder_var.get().strip(), "browser": self.browser_var.get().strip(), "port": self.browser.port,
                   "urls": self.url_text.get("1.0", "end").strip()}
        CONFIG_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf8")

    def append_log(self, text):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with (CONFIG_DIR / "nhat_ky.txt").open("a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except Exception: pass
        self.logs.configure(state="normal")
        self.logs.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def emit(self, key, *args): self.msgs.put((key, args))
    def log(self, text): self.emit("log", text)

    def process_events(self):
        try:
            while True:
                key,args = self.msgs.get_nowait()
                if key == "log": self.append_log(args[0])
                elif key == "progress":
                    done,total,text = args
                    self.progress.configure(maximum=max(1,total), value=done)
                    self.progress_var.set(text)
                elif key == "finished":
                    self.busy=False
                    self.go_btn.configure(state="normal")
                    self.scan_btn.configure(state="normal")
                    self.login_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    if args[0]:
                        messagebox.showinfo("Hoàn tất", args[0])
                elif key == "scan_ok": self.chapters_by_url = args[0]
                elif key == "port": self.browser.port = args[0]; self.save_config()
        except queue.Empty:
            pass
        self.root.after(140, self.process_events)

    def pick_folder(self):
        loc=filedialog.askdirectory(initialdir=self.folder_var.get() if Path(self.folder_var.get()).exists() else str(Path.home()))
        if loc: self.folder_var.set(loc); self.save_config()

    def pick_browser(self):
        loc=filedialog.askopenfilename(title="Chọn file browser.exe của Cốc Cốc", filetypes=[("EXE", "*.exe")])
        if loc: self.browser_var.set(loc); self.save_config()

    def open_folder(self):
        folder = Path(self.folder_var.get()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32": os.startfile(str(folder))
        else: messagebox.showinfo("Vị trí", str(folder))

    def get_urls(self):
        urls = [s.strip() for s in self.url_text.get("1.0","end").splitlines() if s.strip()]
        unique=list(dict.fromkeys(urls))
        for u in unique:
            p=urlparse(u)
            if p.scheme not in ("http","https") or not p.hostname:
                raise ValueError("Link không hợp lệ: " + u)
        return unique

    def get_range(self):
        try:
            low=float(self.start_var.get().replace(",","."))
            high=float(self.end_var.get().replace(",","."))
        except ValueError: raise ValueError("Khoảng chap phải là số, ví dụ 1 và 100")
        if low<0 or high<low: raise ValueError("Khoảng chap không hợp lệ")
        return low,high

    def run_task(self, method):
        def launch():
            message = ""
            pythoncom = None
            try:
                if sys.platform == "win32":
                    import pythoncom
                    pythoncom.CoInitialize()
                message = method() or ""
            except InterruptedError: self.log("Đã dừng theo yêu cầu")
            except Exception as e: self.log("LỖI: " + str(e))
            finally:
                if pythoncom is not None: pythoncom.CoUninitialize()
                self.emit("finished", message)
        threading.Thread(target=launch, daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.append_log("Đang dừng sau bước hiện tại. Không đóng hộp thoại đang lưu nếu chưa hoàn tất.")

    def open_browser(self):
        if not self.prepare(): return
        def task():
            self.browser.ensure()
            self.emit("port", self.browser.port)
            self.log("Đã mở Cốc Cốc Offline (hồ sơ riêng). Hãy đăng nhập thủ công nếu trang yêu cầu.")
        self.run_task(task)

    def scan(self):
        if not self.prepare(): return
        self.run_task(self.do_scan)

    def start(self):
        if not self.prepare(): return
        self.run_task(self.do_save)

    def with_browser(self):
        port=self.browser.ensure()
        self.emit("port", port)
        from playwright.sync_api import sync_playwright
        return port, sync_playwright()

    def discover(self, page, url):
        self.log("Đang quét: " + url)
        if not any((urlparse(url).hostname or "").endswith(d) for d in SUPPORTED_DOMAINS):
            self.log("    Trang chưa có bộ nhận diện riêng; sẽ thử quét theo cấu trúc liên kết chương.")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try: page.wait_for_load_state("networkidle", timeout=11000)
        except Exception: pass
        page.wait_for_timeout(1000)
        anchors=page.locator("a[href]").evaluate_all("els=>els.map(a=>({href:a.href,text:(a.textContent||'').trim()}))")
        chapters=extract_chapters(anchors, url)
        if not chapters:
            raise RuntimeError("Không quét được danh sách chap từ " + url + ". Hãy mở trang thông tin truyện (không phải trang chủ).")
        title=page.locator("h1").first.text_content(timeout=5000) if page.locator("h1").count() else urlparse(url).path.rsplit("/",1)[-1]
        title=sanitize((title or "").strip(), fallback="Truyen")
        low,high=self.get_range_from_local()
        filtered=[x for x in chapters if low<=x["number"]<=high]
        self.log(f"    {title}: tìm thấy {len(chapters)} chap, chọn {len(filtered)} chap trong khoảng {low:g}–{high:g}.")
        return {"title":title,"all":chapters,"selected":filtered}

    def get_range_from_local(self):
        # This gets only captured worker values, not live tkinter widget state.
        return self.range_snapshot

    def do_scan(self):
        # Tkinter values must be captured before entering the worker thread.
        urls=self.urls_snapshot
        port,pw=self.with_browser()
        with pw as playwright:
            browser=playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
            context=browser.contexts[0]
            page=context.new_page()
            result={}
            try:
                for url in urls:
                    if self.stop_event.is_set(): raise InterruptedError()
                    result[url]=self.discover(page,url)
            finally: page.close()
            self.emit("scan_ok",result)
            self.log("Quét xong. Nhấn 'BẮT ĐẦU LƯU' để chạy Ctrl+S theo thứ tự.")

    def do_save(self):
        urls=self.urls_snapshot
        output_root=self.output_snapshot
        port,pw=self.with_browser()
        with pw as playwright:
            browser=playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=15000)
            context=browser.contexts[0]
            page=context.new_page()
            results=[]
            try:
                for url in urls:
                    if self.stop_event.is_set(): raise InterruptedError()
                    info=self.discover(page,url)
                    chapters=info["selected"]
                    if not chapters:
                        self.log("Không có chương phù hợp trong khoảng đã chọn.")
                        continue
                    folder=output_root/sanitize(urlparse(url).hostname or "website")/sanitize(info["title"])
                    folder.mkdir(parents=True, exist_ok=True)
                    successes=[]
                    failures=[]
                    for idx,chapter in enumerate(chapters,1):
                        if self.stop_event.is_set(): raise InterruptedError()
                        num=norm_num(str(chapter["number"]))
                        target=folder/f"Chap_{num}.html"
                        self.emit("progress",idx-1,len(chapters),f"{info['title']}: chap {num} ({idx}/{len(chapters)})")
                        if verified_previous_save(folder,num,target,chapter["url"]):
                            self.log(f"Chap {num}: có checkpoint và đủ ảnh offline; bỏ qua.")
                            successes.append(chapter); write_index(folder,successes,info["title"])
                            continue
                        try:
                            if target.exists():
                                self.log(f"Chap {num}: bản cũ chưa được xác minh; chuyển vào thư mục kiểm tra trước khi tải lại.")
                                backup_partial(target)
                            self.log(f"Chap {num}: đang mở và cuộn trang...")
                            page.goto(chapter["url"],wait_until="domcontentloaded",timeout=60000)
                            try: page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception: pass
                            result=scroll_until_stable(page,self.log,self.stop_event)
                            expected=result["valid"]
                            self.log(f"Chap {num}: {result['ready']} ảnh đã nạp; "
                                     f"yêu cầu ít nhất {math.ceil(expected*0.8)} ảnh offline.")
                            page.bring_to_front()  # Native Ctrl+S must save the scanned chapter tab.
                            time.sleep(0.35)
                            assets=perform_ctrl_s_save(target,self.browser.proc.pid if self.browser.proc else None,
                                                       self.browser.path,self.stop_event,self.log,expected)
                            record_checkpoint(folder,num,expected,assets,chapter["url"])
                            successes.append(chapter)
                            self.log(f"Chap {num}: ✓ HTML + {assets} tài nguyên (kiểm tra offline thêm).")
                            write_index(folder,successes,info["title"])
                        except InterruptedError: raise
                        except Exception as e:
                            failures.append((chapter,e))
                            self.log(f"Chap {num}: ✗ {e}")
                            error_text = str(e).casefold()
                            if any(k in error_text for k in ("hộp thoại", "ctrl+s", "đường dẫn lưu", "cửa sổ cốc cốc", "lưu dưới dạng", "hoàn chỉnh")):
                                self.log("Dừng hàng chờ vì thao tác Ctrl+S không ổn định; cần xử lý lỗi trước.")
                                break
                        self.emit("progress",idx,len(chapters),f"{info['title']}: đã xử lý {idx}/{len(chapters)}")
                    report=folder/"BAO_CAO.txt"
                    lines=["TruyenSaver Coc Coc Offline v"+VERSION,"Truyện: "+info["title"],"Nguồn: "+url,
                           f"Đã lưu: {len(successes)}/{len(chapters)}", "", "LỖI / CHƯƠNG CẦN KIỂM TRA:"]
                    lines.extend(f"Chap {norm_num(str(c['number']))}: {err}" for c,err in failures)
                    report.write_text("\n".join(lines),encoding="utf8")
                    write_index(folder,successes,info["title"])
                    self.log(f"{info['title']}: hoàn thành {len(successes)}/{len(chapters)}. Mở {folder/'MUC_LUC.html'} để đọc offline.")
                    results.append(f"{info['title']}: {len(successes)}/{len(chapters)} chap")
            finally: page.close()
            return ("Đã xử lý xong:\n"+"\n".join(results)+"\nHãy thử mở HTML khi tắt mạng để kiểm tra ảnh.") if results else ""

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno("Đang chạy", "Phần mềm đang làm việc. Bạn muốn dừng và đóng? Hộp thoại lưu có thể còn mở."):
                return
            self.stop_event.set()
        self.save_config()
        self.root.destroy()

    # Snapshot GUI input before worker starts, to avoid tkinter cross-thread access.
    def prepare(self):
        if self.busy: return False
        try:
            self.urls_snapshot=self.get_urls()
            self.range_snapshot=self.get_range()
            self.output_snapshot=Path(self.folder_var.get().strip()).expanduser().resolve()
            if not Path(self.browser_var.get()).is_file():
                raise ValueError("Không tìm thấy browser.exe. Nhấn 'Tìm file…' để chọn Cốc Cốc.")
            self.save_config()
        except Exception as e:
            messagebox.showerror("Kiểm tra thông tin", str(e))
            return False
        self.browser.path=self.browser_var.get()
        self.busy=True
        self.stop_event.clear()
        for button in (self.go_btn,self.scan_btn,self.login_btn): button.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        return True


if __name__ == "__main__":
    root = tk.Tk()
    try: ttk.Style().theme_use("clam")
    except tk.TclError: pass
    OfflineSaverApp(root)
    root.mainloop()
