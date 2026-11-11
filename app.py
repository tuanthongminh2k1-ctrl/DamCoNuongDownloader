"""TruyenPDF Coc Coc v4 - Save readable chapter pages as one PDF.
Uses an installed Coc Coc browser, not a bundled Chrome/Chromium binary.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import tempfile
import threading
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from playwright.sync_api import sync_playwright
from pypdf import PdfReader, PdfWriter

APP_TITLE = "TruyenPDF Cốc Cốc v4"
CONFIG_DIR = Path(os.getenv("APPDATA", Path.home())) / "TruyenPDF_CocCoc"
CONFIG_FILE = CONFIG_DIR / "settings.json"
CHAPTER_RE = re.compile(r"(?:chapter|chap|chương|chuong|ch)[\s_:/.-]*(\d+(?:\.\d+)?)", re.IGNORECASE)


def clean_filename(value: str, limit: int = 100) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:limit].strip(" .") or "Truyen"


def locate_coccoc() -> Path | None:
    """Find browser.exe in common Coc Coc Windows installation locations."""
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")),
        Path(os.environ.get("PROGRAMFILES", "")),
        Path(os.environ.get("PROGRAMFILES(X86)", "")),
    ]
    relative = (
        "CocCoc/Browser/Application/browser.exe",
        "CocCoc/Browser/Application/chrome.exe",
        "CocCoc/Browser/Application/coccoc.exe",
    )
    for base in roots:
        if not str(base) or str(base) == ".":
            continue
        for rel in relative:
            exe = base / rel
            if exe.is_file():
                return exe
    # A user who installed Coc Coc to a custom folder can select it in the UI.
    return None


def load_settings() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_chapter(value: str) -> int | None:
    value = unicodedata.normalize("NFC", value)
    m = CHAPTER_RE.search(value)
    if not m:
        return None
    n = float(m.group(1))
    return int(n) if n.is_integer() else None


def select_chapter_links(links: list[dict], start: int, end: int, base_url: str) -> list[tuple[int, str]]:
    """Pick a unique chapter URL per chapter; ignore other hosts and unrelated sidebars.

    The chapter-list DOM is prioritized by the extractor's ``rank`` field.
    """
    origin = urlparse(base_url).netloc.lower()
    candidates: dict[int, tuple[int, str]] = {}
    for link in links:
        url = str(link.get("url") or "")
        if urlparse(url).scheme not in ("http", "https") or urlparse(url).netloc.lower() != origin:
            continue
        if urlparse(url).path.rstrip("/") == urlparse(base_url).path.rstrip("/"):
            continue
        label = str(link.get("text") or "")
        number = parse_chapter(label) or parse_chapter(urlparse(url).path)
        if number is None or not (start <= number <= end):
            continue
        rank = int(link.get("rank") or 0)
        if number not in candidates or rank > candidates[number][0]:
            candidates[number] = (rank, url)
    return [(n, candidates[n][1]) for n in sorted(candidates)]


# Readable, content-only export; this is printing a browser page to PDF,
# not literally pressing Ctrl+S (Ctrl+S saves web archive/HTML).
FIND_CHAPTERS_JS = r"""() => {
  const priority = '.list-chapter, .chapter-list, #chapter-list, #list-chapter, '
    + '.chapters, .chap-list, .chapter-items, .chapter-list-container, '
    + '[class*="chapter-list"], [id*="chapter-list"]';
  return Array.from(document.querySelectorAll('a[href]')).map(a => ({
    url: a.href,
    text: (a.innerText || a.textContent || a.title || '').trim(),
    rank: a.closest(priority) ? 10 : 0
  }));
}"""

SCROLL_STEP_JS = r"""() => {
  let before = window.scrollY;
  let step = Math.max(500, Math.floor(window.innerHeight * 0.8));
  window.scrollBy(0, step);
  return (window.scrollY <= before) ||
    (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 3);
}"""

# Deliberately save only images already rendered as part of chapter content.
# Rejects tiny/ad assets instead of silently producing an advertising PDF.
CLEAN_FOR_PDF_JS = r"""async () => {
  const readerSelectors = [
    '.reading-detail', '.chapter-content', '.page-chapter', '.viewer-content',
    '.reading-content', '.chapter-reader', '#chapter-content',
    '.entry-content', '.reading-area', '.comic-content', '.chapter-images',
    '.reader-content', '.reader-area', '#chapter-reader'
  ];
  const matches = readerSelectors.map(s => document.querySelector(s))
      .filter(Boolean).sort((a, b) => b.querySelectorAll('img').length - a.querySelectorAll('img').length);
  const region = matches.find(x => x.querySelectorAll('img').length > 0) || document;
  const all = Array.from(region.querySelectorAll('img'));
  const candidates = all.filter(img => {
    const src = img.currentSrc || img.src || '';
    const info = ((img.id || '') + ' ' + (img.className || '') + ' ' + (img.alt || '') + ' ' + src).toLowerCase();
    if (!/^https?:|^data:image\//.test(src)) return false;
    if (/adserver|doubleclick|googleads|advert|banner|pixel|tracking|favicon|sprite|avatar|logo/.test(info)) return false;
    if (img.closest('header, footer, nav, aside, [class*="advert"], [class*="banner"], [id*="advert"], [id*="banner"]')) return false;
    const w = img.naturalWidth || img.width;
    const h = img.naturalHeight || img.height;
    return w >= 320 && h >= 320 && w * h >= 200000;
  });
  if (!candidates.length) return {ok: false, total: all.length, accepted: 0};
  const data = candidates.map(img => ({src: img.currentSrc || img.src, alt: img.alt || ''}));
  document.head.innerHTML = '<meta charset="UTF-8">';
  const css = document.createElement('style');
  css.textContent = '@page{size:A4;margin:0} html,body{padding:0!important;margin:0!important;background:white!important;}'
    + 'main{margin:0 auto;width:100%;max-width:794px;} img{display:block!important;width:auto!important;max-width:100%!important;height:auto!important;margin:0 auto!important;}'
    + '*,*::before,*::after{box-shadow:none!important}';
  document.head.appendChild(css);
  const main = document.createElement('main');
  const images = data.map(d => {const img = document.createElement('img');img.src = d.src;img.alt=d.alt;main.appendChild(img);return img;});
  document.body.replaceChildren(main);
  await Promise.all(images.map(async im => {try{await im.decode()} catch(e){}}));
  return {ok:true,total:all.length,accepted:images.filter(im => im.naturalWidth > 0).length};
}"""


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("800x650")
        root.minsize(700, 570)
        self.events = queue.Queue()
        self.busy = False
        self.stop_requested = False
        previous = load_settings()
        default_folder = Path.home() / "Downloads"

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="TRUYEN PDF · TRÌNH DUYỆT CỐC CỐC", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Tự cuộn chap để nạp ảnh → chỉ giữ ảnh đọc truyện → gộp thành một PDF.", wraplength=740).pack(anchor="w", pady=(5, 15))

        self.url = tk.StringVar(value=previous.get("url", ""))
        self.start = tk.StringVar(value=previous.get("start", "1"))
        self.end = tk.StringVar(value=previous.get("end", "100"))
        self.out = tk.StringVar(value=previous.get("output", str(default_folder)))
        self.browser = tk.StringVar(value=previous.get("browser", str(locate_coccoc() or "")))
        self.show_browser = tk.BooleanVar(value=previous.get("show_browser", False))

        self.add_labeled_entry(frame, "Link trang truyện (trang có danh sách chap)", self.url)
        limits = ttk.Frame(frame)
        limits.pack(fill="x", pady=(3, 10))
        ttk.Label(limits, text="Từ chap:").pack(side="left")
        ttk.Entry(limits, textvariable=self.start, width=8).pack(side="left", padx=(6, 24))
        ttk.Label(limits, text="Đến chap:").pack(side="left")
        ttk.Entry(limits, textvariable=self.end, width=8).pack(side="left", padx=6)

        path_line = ttk.Frame(frame)
        path_line.pack(fill="x", pady=6)
        ttk.Label(path_line, text="THƯ MỤC LƯU PDF", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        choose = ttk.Frame(path_line)
        choose.pack(fill="x", pady=4)
        ttk.Entry(choose, textvariable=self.out).pack(side="left", fill="x", expand=True)
        ttk.Button(choose, text="Chọn thư mục…", command=self.choose_folder).pack(side="left", padx=(7, 0))
        ttk.Button(choose, text="Mở thư mục", command=self.open_folder).pack(side="left", padx=(7, 0))

        browser_line = ttk.Frame(frame)
        browser_line.pack(fill="x", pady=(9, 5))
        ttk.Label(browser_line, text="ĐƯỜNG DẪN CỐC CỐC", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        browser_fields = ttk.Frame(browser_line)
        browser_fields.pack(fill="x", pady=4)
        ttk.Entry(browser_fields, textvariable=self.browser).pack(side="left", fill="x", expand=True)
        ttk.Button(browser_fields, text="Tìm Cốc Cốc…", command=self.choose_browser).pack(side="left", padx=(7, 0))

        ttk.Checkbutton(frame, text="Hiện cửa sổ Cốc Cốc khi tải (có thể không hỗ trợ xuất PDF trên một số bản)", variable=self.show_browser).pack(anchor="w", pady=(5, 12))
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(0, 10))
        self.download_btn = ttk.Button(controls, text="TẢI VÀ GỘP THÀNH 1 PDF", command=self.start_download)
        self.download_btn.pack(side="left")
        ttk.Button(controls, text="Dừng sau chap hiện tại", command=self.request_stop).pack(side="left", padx=10)
        self.progress = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        ttk.Label(frame, text="Nhật ký tải:", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(12, 4))
        self.log_box = tk.Text(frame, height=12, wrap="word", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)
        self.root.after(120, self.flush_events)

    @staticmethod
    def add_labeled_entry(parent, label, var):
        ttk.Label(parent, text=label).pack(anchor="w")
        ttk.Entry(parent, textvariable=var).pack(fill="x", pady=(4, 8))

    def choose_folder(self):
        start = self.out.get().strip()
        folder = filedialog.askdirectory(parent=self.root, initialdir=start if Path(start).is_dir() else str(Path.home()), title="Chọn nơi lưu file PDF")
        if folder:
            self.out.set(folder)
            self.remember()

    def open_folder(self):
        folder = Path(self.out.get().strip())
        if not folder.is_dir():
            messagebox.showwarning("Chưa có thư mục", "Hãy chọn thư mục có sẵn trước.")
            return
        if sys.platform == "win32":
            os.startfile(str(folder))
        else:
            messagebox.showinfo("Đường dẫn thư mục", str(folder))

    def choose_browser(self):
        path = filedialog.askopenfilename(parent=self.root, title="Chọn browser.exe của Cốc Cốc", filetypes=[("Ứng dụng Windows", "*.exe"), ("Tất cả", "*.*")])
        if path:
            self.browser.set(path)
            self.remember()

    def remember(self):
        save_settings({"url": self.url.get(), "start": self.start.get(), "end": self.end.get(), "output": self.out.get(), "browser": self.browser.get(), "show_browser": self.show_browser.get()})

    def request_stop(self):
        self.stop_requested = True
        self.put_log("Đã yêu cầu dừng sau chap đang xử lý.")

    def put_log(self, message):
        self.events.put(("log", message))

    def flush_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log_box.insert("end", str(value) + "\n")
                    self.log_box.see("end")
                elif kind == "progress":
                    self.progress["value"] = value
                elif kind == "done":
                    self.busy = False
                    self.download_btn.config(state="normal")
                    messagebox.showinfo("TruyenPDF", value)
        except queue.Empty:
            pass
        self.root.after(120, self.flush_events)

    def start_download(self):
        if self.busy:
            return
        try:
            a, b = int(self.start.get()), int(self.end.get())
            if not (1 <= a <= b <= 2000):
                raise ValueError("Chọn chap từ 1 đến 2000, số bắt đầu không lớn hơn số kết thúc")
            url = self.url.get().strip()
            if urlparse(url).scheme not in ("http", "https"):
                raise ValueError("Link phải bắt đầu bằng https:// hoặc http://")
            browser_path = Path(self.browser.get().strip())
            if not browser_path.is_file() or browser_path.suffix.lower() != ".exe":
                raise ValueError("Không tìm thấy Cốc Cốc. Hãy chọn browser.exe bằng nút 'Tìm Cốc Cốc…'")
            output_dir = Path(self.out.get().strip())
            if not output_dir.is_dir():
                raise ValueError("Thư mục lưu chưa tồn tại. Nhấn 'Chọn thư mục…' để chọn")
            self.remember()
        except Exception as exc:
            messagebox.showerror("Chưa thể tải", str(exc))
            return
        self.stop_requested = False
        self.busy = True
        self.progress["value"] = 0
        self.download_btn.config(state="disabled")
        threading.Thread(target=self.run_download, args=(url, a, b, browser_path, output_dir, self.show_browser.get()), daemon=True).start()

    def run_download(self, url: str, start: int, end: int, browser_path: Path, output_dir: Path, show_browser: bool):
        writer = PdfWriter()
        success = 0
        missed = []
        try:
            self.put_log("Khởi động Cốc Cốc: " + str(browser_path))
            self.put_log("Thư mục lưu: " + str(output_dir))
            with sync_playwright() as pw:
                # Separate profile avoids taking over the user's own Coc Coc session.
                profile = CONFIG_DIR / "browser-profile"
                profile.mkdir(parents=True, exist_ok=True)
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    executable_path=str(browser_path),
                    headless=not show_browser,
                    viewport={"width": 1100, "height": 900},
                    args=["--no-first-run", "--no-default-browser-check"],
                    accept_downloads=False,
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    self.put_log("Mở trang danh sách chương…")
                    page.goto(url, wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(2000)
                    title = clean_filename(page.title().split(" | ")[0].split(" - ")[0], 80)
                    links = page.evaluate(FIND_CHAPTERS_JS)
                    chapters = select_chapter_links(links, start, end, page.url)
                    self.put_log(f"Nhận diện {len(chapters)} chương trong khoảng {start}–{end}.")
                    if not chapters:
                        # A standalone chapter link is useful only for one-chapter requests.
                        single = parse_chapter(page.url)
                        if start == end and single == start:
                            chapters = [(start, page.url)]
                        else:
                            raise RuntimeError("Không nhận diện được danh sách chap. Hãy dán trang thông tin truyện có danh sách chương (không dán trang chủ).")
                    with tempfile.TemporaryDirectory(prefix="truyenpdf_") as tmpdir:
                        for index, (number, chapter_url) in enumerate(chapters):
                            if self.stop_requested:
                                self.put_log("Dừng theo yêu cầu.")
                                break
                            self.put_log(f"Chap {number}: mở bằng Cốc Cốc…")
                            try:
                                page.goto(chapter_url, wait_until="domcontentloaded", timeout=90000)
                                page.wait_for_timeout(1600)
                                for _ in range(160):
                                    if self.stop_requested:
                                        break
                                    finished = page.evaluate(SCROLL_STEP_JS)
                                    page.wait_for_timeout(230)
                                    if finished:
                                        break
                                page.wait_for_timeout(1200)
                                stats = page.evaluate(CLEAN_FOR_PDF_JS)
                                if not stats["ok"] or stats["accepted"] < 1:
                                    raise RuntimeError(f"Không thấy ảnh truyện hợp lệ (kiểm tra {stats['total']} ảnh).")
                                pdf_path = Path(tmpdir) / f"chap_{number:04d}.pdf"
                                page.emulate_media(media="screen")
                                page.pdf(path=str(pdf_path), format="A4", print_background=True,
                                         margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
                                if pdf_path.stat().st_size < 5000:
                                    raise RuntimeError("PDF quá nhỏ, có thể ảnh chưa tải xong")
                                pdf = PdfReader(str(pdf_path))
                                if not pdf.pages:
                                    raise RuntimeError("PDF không có trang")
                                writer.append(pdf)
                                success += 1
                                self.put_log(f"  ✓ {stats['accepted']} ảnh, {len(pdf.pages)} trang PDF")
                            except Exception as exc:
                                missed.append(number)
                                self.put_log(f"  ✗ Chap {number}: {str(exc)[:200]}")
                            self.events.put(("progress", round((index + 1) * 100 / len(chapters))))
                    if success == 0:
                        raise RuntimeError("Không xuất được chương nào. Kiểm tra nhật ký và thử riêng 1 chap.")
                    output_path = output_dir / f"{title}_chap_{chapters[0][0]}-{chapters[-1][0]}.pdf"
                    suffix = 2
                    while output_path.exists():
                        output_path = output_dir / f"{title}_chap_{chapters[0][0]}-{chapters[-1][0]}_{suffix}.pdf"
                        suffix += 1
                    with output_path.open("wb") as f:
                        writer.write(f)
                    self.put_log("ĐÃ LƯU: " + str(output_path))
                    self.events.put(("done", f"Đã gộp {success}/{len(chapters)} chap.\n\nPDF: {output_path}\n" + (f"Chap bị lỗi: {', '.join(map(str,missed))}\n" if missed else "") + "Kiểm tra PDF trước khi xóa dữ liệu khác."))
                finally:
                    context.close()
        except Exception as exc:
            self.put_log("LỖI: " + repr(exc))
            self.events.put(("done", "Không hoàn tất: " + str(exc)))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
