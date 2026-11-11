"""TruyenSaver 1.3: background Cốc Cốc reader archive (.mhtml).

Runs a private browser profile via the Chrome DevTools Protocol. No keyboard or
mouse automation. Saves the Chromium MHTML snapshot, not native Ctrl+S HTML+_files.
The user should verify a sample chapter offline before large batch downloads.
"""
from __future__ import annotations

import html
import json
import math
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

APP_NAME = 'TruyenSaver Cốc Cốc – Chạy nền'
VERSION = '1.3.0'
CONFIG_DIR = Path(os.getenv('APPDATA') or Path.home()) / 'TruyenSaverCocCoc'
PROFILE_DIR = CONFIG_DIR / 'CocCoc_Profile_Offline'
CONFIG_FILE = CONFIG_DIR / 'settings_v1_3.json'
CHAPTER_RE = re.compile(r'(?:chap(?:ter)?|chương|chuong)\s*[:#\-_. ]*\s*(\d+(?:[.,]\d+)?)', re.I)
SUPPORTED = ('damconuong.cool', 'sayhentai.cx')


def sanitize(text: str, fallback='Truyen', limit=90) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(text)).strip(' .')
    return cleaned[:limit].rstrip(' .') or fallback


def norm_num(value: float | str) -> str:
    x = float(str(value).replace(',', '.'))
    return f'{int(x):03d}' if x.is_integer() else f'{x:.2f}'.rstrip('0').rstrip('.')


def chapter_from_anchor(text: str, link: str, series_url: str) -> float | None:
    source = urlparse(series_url)
    parsed = urlparse(link)
    if (source.hostname or '').removeprefix('www.') != (parsed.hostname or '').removeprefix('www.'):
        return None
    source_path = source.path.rstrip('/').lower()
    chapter_path = parsed.path.rstrip('/').lower()
    slug = source_path.rsplit('/', 1)[-1].removesuffix('.html')
    if 'sayhentai' in (parsed.hostname or ''):
        good = chapter_path.startswith(source_path + '/') or chapter_path.startswith(source_path.removesuffix('.html') + '-') or (slug in chapter_path and chapter_path != source_path)
    else:
        good = chapter_path.startswith(source_path + '/')
    if not good:
        return None
    names = CHAPTER_RE.findall((text or '') + ' ' + chapter_path.replace('-', ' ').replace('_', ' '))
    try:
        return float(names[-1].replace(',', '.')) if names else None
    except ValueError:
        return None


def extract_chapters(anchors: list[dict], series_url: str) -> list[dict]:
    unique = {}
    for a in anchors:
        url = urljoin(series_url, a.get('href') or '')
        num = chapter_from_anchor(a.get('text') or '', url, series_url)
        if num is not None and num not in unique:
            unique[num] = {'number': num, 'url': url}
    return [unique[num] for num in sorted(unique)]


def find_coccoc() -> str | None:
    candidates = [
        Path(os.getenv('LOCALAPPDATA', '')) / 'CocCoc/Browser/Application/browser.exe',
        Path(os.getenv('PROGRAMFILES', '')) / 'CocCoc/Browser/Application/browser.exe',
        Path(os.getenv('PROGRAMFILES(X86)', '')) / 'CocCoc/Browser/Application/browser.exe',
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


def random_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def browser_ready(port: int) -> bool:
    from urllib.request import urlopen
    try:
        with urlopen(f'http://127.0.0.1:{port}/json/version', timeout=0.7) as response:
            return response.status == 200
    except Exception:
        return False


@dataclass
class BrowserHandle:
    path: str
    proc: subprocess.Popen | None = None
    port: int | None = None
    mode: str | None = None

    def shutdown(self):
        """Close only the private browser profile started by this app."""
        if self.port and browser_ready(self.port):
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as driver:
                    connected = driver.chromium.connect_over_cdp(f'http://127.0.0.1:{self.port}', timeout=6000)
                    connected.new_browser_cdp_session().send('Browser.close')
            except Exception:
                if self.proc and self.proc.poll() is None:
                    self.proc.terminate()
            for _ in range(18):
                if not browser_ready(self.port): break
                time.sleep(0.2)
        if self.port and browser_ready(self.port):
            raise RuntimeError('Không thể đóng hồ sơ Cốc Cốc Offline. Hãy đóng thủ công trước khi chuyển chế độ.')
        self.port = None
        self.proc = None
        self.mode = None

    def ensure(self, *, visible=False) -> int:
        requested = 'visible' if visible else 'headless'
        if self.port and browser_ready(self.port):
            if self.mode != requested:
                self.shutdown()  # Log-in window -> background, or reverse.
            else:
                return self.port
        self.port = random_port()
        self.mode = requested
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        cmd = [self.path, f'--remote-debugging-port={self.port}',
               '--remote-debugging-address=127.0.0.1', f'--user-data-dir={PROFILE_DIR}',
               '--no-first-run', '--disable-background-timer-throttling',
               '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows']
        if not visible:
            cmd += ['--headless=new', '--window-size=1400,1000']
        if sys.platform != 'win32' and hasattr(os, 'geteuid') and os.geteuid() == 0:
            cmd.append('--no-sandbox')  # local Linux smoke tests only
        cmd.append('about:blank')
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if browser_ready(self.port):
                return self.port
            if self.proc.poll() is not None:
                raise RuntimeError('Cốc Cốc không thể khởi động. Kiểm tra đường dẫn browser.exe và đóng ứng dụng cũ.')
            time.sleep(0.25)
        raise RuntimeError('Không kết nối được Cốc Cốc ở chế độ ' + requested + '. Đóng mọi cửa sổ Cốc Cốc Offline rồi thử lại.')


# Detect candidates in the READING CONTAINER only, not across ads, logo or sidebar.
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
 let matches=[];
 for (const [rank, sel] of sels.entries()) {
  for (const el of document.querySelectorAll(sel)) {
   let n=el.querySelectorAll('img').length;
   if(n>0) matches.push({el,sel,n,rank});
  }
 }
 matches.sort((a,b)=> (a.n>=2?0:1)-(b.n>=2?0:1) || a.rank-b.rank || b.n-a.n);
 const root=matches[0]?.el, selector=matches[0]?.sel||'';
 let imgs = [...(root?.querySelectorAll('img')||[])];
 const details=imgs.map(i=>{
   const url=i.currentSrc||i.getAttribute('data-src')||i.src||'';
   const pending=i.getAttribute('data-src')||i.getAttribute('data-original')||i.getAttribute('data-lazy-src')||'';
   const placeholder=/^(data:)|placeholder|loading\.gif|lazy\.gif/i.test(url);
   const ready=!!(i.complete&&i.naturalWidth>0&&i.naturalHeight>0&&!(pending&&placeholder));
   const valid=ready&&i.naturalWidth>=100&&i.naturalHeight>=100;
   return {url,ready,valid,w:i.naturalWidth||0,h:i.naturalHeight||0};
 });
 return {
  selector, total:details.length, ready:details.filter(x=>x.ready).length,
  missing:details.filter(x=>!x.ready).length,
  valid:details.filter(x=>x.valid).length,
  urls:[...new Set(details.filter(x=>x.valid&&/^https?:/i.test(x.url)).map(x=>x.url))],
  inline:details.filter(x=>x.valid&&x.url.startsWith('data:')).length,
  height:document.documentElement.scrollHeight,
  atBottom:scrollY+innerHeight>=document.documentElement.scrollHeight-15,
  viewport:innerHeight
 };
}"""


def scroll_and_check(page, log, stop: threading.Event, timeout=210) -> dict:
    """Fast first pass, slower recovery pass, 3s stable after bottom.

    Only validates images exposed in reader DOM; websites can hide resources.
    """
    started = time.monotonic()
    last: dict = {}
    for attempt in range(3):
        page.evaluate('window.scrollTo(0,0)')
        time.sleep(0.22)
        # A viewport-sized stride is faster but still intersects lazy-load thresholds.
        delay = (0.14, 0.35, 0.65)[attempt]
        while time.monotonic() - started < timeout:
            if stop.is_set(): raise InterruptedError('Đã dừng')
            info = page.evaluate(READER_SCAN_JS)
            last=info
            if not info['selector']:
                if time.monotonic()-started>16:
                    raise RuntimeError('Không nhận diện được khu vực đọc truyện, có thể cần đăng nhập hoặc website đổi cấu trúc.')
                time.sleep(0.7)
                continue
            if info['atBottom']:
                break
            page.evaluate('window.scrollBy(0,Math.max(500, Math.round(innerHeight*0.95)))')
            time.sleep(delay)
        # Monitor until images AND document geometry remain stable for ~3 seconds.
        stable_since = None
        prev = None
        stuck_since = time.monotonic()
        last_missing = None
        while time.monotonic()-started < timeout:
            if stop.is_set(): raise InterruptedError('Đã dừng')
            info=page.evaluate(READER_SCAN_JS)
            last=info
            state=(info['total'],info['ready'],info['missing'],info['height'])
            if info['atBottom'] and info['total'] and info['missing']==0 and info['valid']>0:
                if state == prev:
                    if stable_since is None: stable_since = time.monotonic()
                    if time.monotonic()-stable_since >= 3.0:
                        log(f"    Đã nạp {info['ready']}/{info['total']} ảnh vùng đọc (lượt {attempt+1}).")
                        return info
                else:
                    stable_since = None
            else:
                stable_since = None
            prev=state
            if last_missing != (info['missing'],info['ready'],info['total']):
                stuck_since = time.monotonic()
                last_missing = (info['missing'],info['ready'],info['total'])
            if info['missing'] and stable_since is None:
                # Trigger reloading of image placeholders and lazy in-view media.
                page.evaluate("""() => {
                  document.querySelectorAll('img').forEach(i=>{
                   if(i.loading==='lazy') i.loading='eager';
                  });
                }""")
            if not info['atBottom']:
                break
            if time.monotonic()-started >= timeout: break
            time.sleep(0.45)
            # Try again if pending images haven't improved in ~6s.
            if info['missing'] and time.monotonic()-stuck_since > 6+attempt*4:
                break
        log(f"    Chưa đủ ảnh sau lượt {attempt+1}; thử cuộn lại chậm hơn...")
    raise RuntimeError(f"Hết thời gian/chưa tải đủ ảnh trong vùng đọc: {last.get('ready',0)}/{last.get('total',0)}. Không lưu chap này.")


def mhtml_image_count(snapshot: bytes) -> tuple[int, set[str]]:
    """Count embedded image MIME parts; never assume raw .mhtml is complete."""
    msg = BytesParser(policy=policy.default).parsebytes(snapshot)
    locations: set[str] = set()
    count = 0
    for part in msg.walk():
        if part.get_content_maintype() == 'image':
            count += 1
            loc = part.get('Content-Location')
            if loc:
                locations.add(loc.strip().strip('<>'))
    return count, locations


def verify_mhtml(data: bytes, expected_images: int, known_urls: list[str]) -> tuple[bool, int, str]:
    if len(data)<1500 or b'multipart/related' not in data[:1500].lower():
        return False, 0, 'MHTML rỗng hoặc không đúng định dạng'
    try:
        count, locations = mhtml_image_count(data)
    except Exception as ex:
        return False, 0, 'Không đọc được MHTML: '+str(ex)
    if expected_images <= 0:
        return False, count, 'Không biết số ảnh cần lưu'
    # When readers use inline/base64 images, the HTML body holds the image itself.
    inline_count = sum(1 for link in known_urls if link.startswith('data:'))
    threshold = max(1, math.ceil(expected_images*.9))
    if count + inline_count < threshold:
        return False,count, f'Tệp chỉ chứa {count} ảnh; dự kiến ít nhất {threshold}/{expected_images}'
    # Count-only is best-effort because MHTML can include icons/ads.
    return True,count,'Đã ghi đủ số tài nguyên ảnh sơ bộ; cần mở offline kiểm tra'


def save_mhtml(page, path: Path, expected: dict, log) -> int:
    """Snapshot using CDP: no Ctrl+S dialog, focus, keyboard or mouse input."""
    session=page.context.new_cdp_session(page)
    try:
        out=session.send('Page.captureSnapshot', {'format':'mhtml'})
    finally:
        try: session.detach()
        except Exception: pass
    data=out.get('data','').encode('utf-8')
    urls=expected.get('urls',[])+['data:']*expected.get('inline',0)
    ok,count,message=verify_mhtml(data,expected['valid'],urls)
    if not ok:
        raise RuntimeError('MHTML chưa đủ: '+message)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.mhtml.tmp')
    tmp.write_bytes(data)
    os.replace(tmp,path)
    log(f'    Đã lưu {path.name} ({len(data)//1024} KB; {count} tài nguyên ảnh).')
    return count


def checkpoint_file(folder: Path) -> Path:
    return folder/'TIEN_DO_LUU.json'


def read_checkpoint(folder: Path) -> dict:
    try:
        data=json.loads(checkpoint_file(folder).read_text(encoding='utf-8'))
        return data if isinstance(data,dict) else {}
    except (ValueError,OSError,TypeError):
        return {}


def completed_before(folder: Path, num: str, path: Path, source: str) -> bool:
    entry=read_checkpoint(folder).get(num)
    if not isinstance(entry,dict) or entry.get('url')!=source or entry.get('version')!=VERSION:
        return False
    return path.exists() and path.stat().st_size>1500


def record_checkpoint(folder: Path, num: str, expected: int, saved: int, source: str):
    data=read_checkpoint(folder)
    data[num]={'url':source,'expected_images':expected,'mhtml_images':saved,
               'saved_at':time.strftime('%Y-%m-%d %H:%M:%S'),'version':VERSION}
    tmp=folder/'TIEN_DO_LUU.json.tmp'
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(tmp,checkpoint_file(folder))


def write_index(folder: Path, chapters: list[dict], title: str):
    items=[]
    for item in sorted(chapters,key=lambda c:c['number']):
        num=norm_num(item['number'])
        dest=folder/f'Chap_{num}.mhtml'
        if dest.exists():
            items.append(f'<li><a href="{html.escape(dest.name,quote=True)}">Chap {html.escape(num)}</a></li>')
    markup=("<!doctype html><html lang='vi'><meta charset='utf-8'><title>Mục lục truyện offline</title>"
       "<style>body{font:18px Arial;max-width:760px;margin:30px auto;padding:14px}li{margin:9px 0}</style>"
       f"<h2>{html.escape(title)}</h2><p>Chọn chương đã lưu. Để xác nhận đầy đủ, mở thử khi ngắt mạng.</p>"
       '<ol>'+''.join(items)+'</ol></html>')
    (folder/'MUC_LUC.html').write_text(markup,encoding='utf-8')


class App:
    def __init__(self, root):
        self.root=root
        self.root.title(f'{APP_NAME} v{VERSION}')
        self.root.geometry('850x740')
        self.root.minsize(720,650)
        self.eventq=queue.Queue()
        self.stop_event=threading.Event()
        self.busy=False
        self.settings=self.read_settings()
        self.browser=BrowserHandle(self.settings.get('browser') or find_coccoc() or '')
        self.folder_var=tk.StringVar(value=self.settings.get('folder') or str(Path.home()/'Downloads'/'Truyen_Offline'))
        self.browser_var=tk.StringVar(value=self.browser.path)
        self.start_var=tk.StringVar(value=str(self.settings.get('start','1')))
        self.end_var=tk.StringVar(value=str(self.settings.get('end','100')))
        self.status_var=tk.StringVar(value='Sẵn sàng')
        self.build_ui()
        self.links.insert('1.0',self.settings.get('links') or 'https://damconuong.cool/truyen/ban-hoc')
        self.root.after(120,self.handle_events)
        self.root.protocol('WM_DELETE_WINDOW',self.on_close)

    def build_ui(self):
        base=ttk.Frame(self.root,padding=17)
        base.pack(fill='both',expand=True)
        ttk.Label(base,text='TruyenSaver – Cốc Cốc chạy nền',font=('Segoe UI',19,'bold')).pack(anchor='w')
        ttk.Label(base,text='Không dùng chuột/phím để lưu • Cuộn thông minh • Lưu MHTML đọc offline',foreground='#36516b').pack(anchor='w',pady=(3,13))
        ttk.Label(base,text='Link trang thông tin truyện (mỗi dòng 1 truyện, có thể từ 2 website):').pack(anchor='w')
        self.links=tk.Text(base,height=3,wrap='word',font=('Segoe UI',10))
        self.links.pack(fill='x',pady=(4,10))
        rr=ttk.Frame(base);rr.pack(fill='x',pady=3)
        ttk.Label(rr,text='Từ chap').pack(side='left',padx=(0,6))
        ttk.Entry(rr,textvariable=self.start_var,width=9).pack(side='left',padx=(0,22))
        ttk.Label(rr,text='Đến chap').pack(side='left',padx=(0,6))
        ttk.Entry(rr,textvariable=self.end_var,width=9).pack(side='left')
        ttk.Label(base,text='Thư mục lưu:').pack(anchor='w',pady=(8,0))
        fr=ttk.Frame(base);fr.pack(fill='x',pady=(4,8))
        ttk.Entry(fr,textvariable=self.folder_var).pack(side='left',fill='x',expand=True)
        ttk.Button(fr,text='Chọn thư mục…',command=self.pick_folder).pack(side='left',padx=(7,0))
        ttk.Button(fr,text='Mở thư mục',command=self.open_folder).pack(side='left',padx=(7,0))
        ttk.Label(base,text='Đường dẫn Cốc Cốc (browser.exe):').pack(anchor='w')
        fb=ttk.Frame(base);fb.pack(fill='x',pady=(4,10))
        ttk.Entry(fb,textvariable=self.browser_var).pack(side='left',fill='x',expand=True)
        ttk.Button(fb,text='Tìm file…',command=self.pick_browser).pack(side='left',padx=(7,0))
        toolbar=ttk.Frame(base);toolbar.pack(fill='x',pady=(4,12))
        self.login_btn=ttk.Button(toolbar,text='Mở Cốc Cốc / đăng nhập',command=self.login)
        self.login_btn.pack(side='left',padx=(0,7))
        self.scan_btn=ttk.Button(toolbar,text='Quét chap',command=self.scan)
        self.scan_btn.pack(side='left',padx=(0,7))
        self.start_btn=ttk.Button(toolbar,text='BẮT ĐẦU LƯU NỀN',command=self.start)
        self.start_btn.pack(side='left',padx=(0,7))
        self.stop_btn=ttk.Button(toolbar,text='Dừng',command=self.stop,state='disabled')
        self.stop_btn.pack(side='left')
        ttk.Label(base,textvariable=self.status_var).pack(anchor='w',pady=(0,6))
        self.progress=ttk.Progressbar(base,mode='determinate')
        self.progress.pack(fill='x',pady=(0,10))
        ttk.Label(base,text='Nhật ký:',font=('Segoe UI',10,'bold')).pack(anchor='w')
        self.log_box=scrolledtext.ScrolledText(base,height=15,state='disabled',font=('Consolas',9),wrap='word')
        self.log_box.pack(fill='both',expand=True,pady=(5,8))
        ttk.Label(base,text='Chạy nền không chiếm chuột/phím. MHTML khác HTML + _files; hãy kiểm tra offline 1–2 chap.',foreground='#9a3412').pack(anchor='w')
        self.append_log('Sẵn sàng. Nếu trang yêu cầu tài khoản, đăng nhập bằng Cốc Cốc riêng rồi đóng cửa sổ trước khi tải nền.')

    @staticmethod
    def read_settings():
        try: return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        except (OSError, ValueError): return {}

    def save_settings(self):
        CONFIG_DIR.mkdir(parents=True,exist_ok=True)
        payload={'folder':self.folder_var.get().strip(),'browser':self.browser_var.get().strip(),
                 'start':self.start_var.get().strip(),'end':self.end_var.get().strip(),
                 'links':self.links.get('1.0','end').strip()}
        CONFIG_FILE.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')

    def pick_folder(self):
        choice=filedialog.askdirectory(initialdir=self.folder_var.get() if Path(self.folder_var.get()).is_dir() else str(Path.home()))
        if choice: self.folder_var.set(choice);self.save_settings()

    def pick_browser(self):
        choice=filedialog.askopenfilename(title='Chọn browser.exe Cốc Cốc',filetypes=[('EXE','*.exe')])
        if choice: self.browser_var.set(choice);self.save_settings()

    def open_folder(self):
        folder=Path(self.folder_var.get()).expanduser()
        folder.mkdir(parents=True,exist_ok=True)
        if sys.platform=='win32': os.startfile(str(folder))
        else: messagebox.showinfo('Thư mục',str(folder))

    def append_log(self,s):
        self.log_box.configure(state='normal')
        self.log_box.insert('end',f'[{time.strftime("%H:%M:%S")}] {s}\n')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    def emit(self,key,*args): self.eventq.put((key,args))
    def log(self,s): self.emit('log',s)

    def handle_events(self):
        try:
            while True:
                key,args=self.eventq.get_nowait()
                if key=='log':self.append_log(args[0])
                elif key=='progress':
                    done,total,title=args
                    self.progress.configure(maximum=max(1,total),value=done)
                    self.status_var.set(title)
                elif key=='finished':
                    self.busy=False
                    for b in (self.login_btn,self.scan_btn,self.start_btn):b.configure(state='normal')
                    self.stop_btn.configure(state='disabled')
                    if args[0]:messagebox.showinfo('Hoàn thành',args[0])
        except queue.Empty:
            pass
        self.root.after(120,self.handle_events)

    def snapshot_fields(self):
        links=list(dict.fromkeys(x.strip() for x in self.links.get('1.0','end').splitlines() if x.strip()))
        if not links:raise ValueError('Chưa nhập link bộ truyện')
        for link in links:
            u=urlparse(link)
            if u.scheme not in ('http','https') or not u.hostname:raise ValueError('Link không hợp lệ: '+link)
        try:
            start=float(self.start_var.get().replace(',','.'))
            end=float(self.end_var.get().replace(',','.'))
        except ValueError:raise ValueError('Khoảng chap phải là số')
        if start < 0 or end < start:raise ValueError('Khoảng chap không hợp lệ')
        if not Path(self.browser_var.get()).is_file():
            raise ValueError('Không tìm thấy browser.exe của Cốc Cốc. Nhấn Tìm file…')
        self.browser.path=self.browser_var.get()
        self.range=(start,end)
        self.url_list=links
        self.output_root=Path(self.folder_var.get()).expanduser().resolve()
        self.save_settings()

    def run_worker(self,func):
        if self.busy:return
        try:self.snapshot_fields()
        except Exception as exc:
            messagebox.showerror('Kiểm tra thông tin',str(exc));return
        self.busy=True
        self.stop_event.clear()
        for b in (self.login_btn,self.scan_btn,self.start_btn):b.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        def worker():
            result=''
            try:result=func() or ''
            except InterruptedError:self.log('Đã dừng theo yêu cầu.')
            except Exception as exc:self.log('LỖI: '+str(exc))
            finally:self.emit('finished',result)
        threading.Thread(target=worker,daemon=True).start()

    def stop(self):
        self.stop_event.set()
        self.append_log('Đang dừng ở điểm an toàn tiếp theo…')

    def login(self):
        self.run_worker(self.login_task)

    def login_task(self):
        self.browser.ensure(visible=True)
        self.log('Đã mở Cốc Cốc để đăng nhập. Bấm Quét/Bắt đầu sẽ tự chuyển về chạy nền sau khi bạn hoàn tất đăng nhập.')

    def scan(self):self.run_worker(self.scan_task)
    def start(self):self.run_worker(self.save_task)

    def discovery(self,page,url):
        self.log('Quét chương: '+url)
        page.goto(url,wait_until='domcontentloaded',timeout=45000)
        try:page.wait_for_load_state('networkidle',timeout=5500)
        except Exception:pass
        page.wait_for_timeout(450)
        anchors=page.locator('a[href]').evaluate_all('es=>es.map(a=>({href:a.href,text:(a.textContent||"").trim()}))')
        chapters=extract_chapters(anchors,url)
        if not chapters:raise RuntimeError('Không tìm thấy chap; hãy dán link trang thông tin bộ truyện hoặc đăng nhập nếu cần.')
        title=page.locator('h1').first.text_content(timeout=3000) if page.locator('h1').count() else urlparse(url).path.rstrip('/').rsplit('/',1)[-1]
        title=sanitize((title or '').strip())
        chapters=[x for x in chapters if self.range[0]<=x['number']<=self.range[1]]
        self.log(f'    {title}: tìm thấy {len(chapters)} chap trong khoảng {self.range[0]:g}–{self.range[1]:g}.')
        return title,chapters

    def connected(self):
        from playwright.sync_api import sync_playwright
        port=self.browser.ensure(visible=False)
        return port,sync_playwright()

    def scan_task(self):
        port,pw=self.connected()
        with pw as driver:
            browser=driver.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
            page=browser.contexts[0].new_page()
            try:
                for url in self.url_list:
                    if self.stop_event.is_set():raise InterruptedError()
                    self.discovery(page,url)
                self.log('Quét hoàn thành. Có thể bấm BẮT ĐẦU LƯU NỀN.')
            finally:page.close()

    def save_task(self):
        port,pw=self.connected()
        total_success=0
        results=[]
        with pw as driver:
            browser=driver.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
            page=browser.contexts[0].new_page()
            try:
                for url in self.url_list:
                    if self.stop_event.is_set():raise InterruptedError()
                    title,chapters=self.discovery(page,url)
                    folder=self.output_root/sanitize(urlparse(url).hostname or 'Web')/title
                    folder.mkdir(parents=True,exist_ok=True)
                    saved=[]
                    failed=[]
                    for idx,item in enumerate(chapters,1):
                        if self.stop_event.is_set():raise InterruptedError()
                        num=norm_num(item['number'])
                        target=folder/f'Chap_{num}.mhtml'
                        self.emit('progress',idx-1,len(chapters),f'{title} – chap {num} ({idx}/{len(chapters)})')
                        if completed_before(folder,num,target,item['url']):
                            self.log(f'Chap {num}: đã lưu và có checkpoint; bỏ qua.')
                            saved.append(item)
                            write_index(folder,saved,title)
                            continue
                        try:
                            self.log(f'Chap {num}: đang mở và cuộn nhanh theo ảnh…')
                            page.goto(item['url'],wait_until='domcontentloaded',timeout=60000)
                            info=scroll_and_check(page,self.log,self.stop_event)
                            if self.stop_event.is_set():raise InterruptedError()
                            try:
                                count=save_mhtml(page,target,info,self.log)
                            except RuntimeError as ex:
                                self.log(f'    Ảnh trong MHTML chưa đủ ({ex}); thử cuộn và chụp lại 1 lần…')
                                page.evaluate('window.scrollTo(0,0)')
                                page.wait_for_timeout(1200)
                                info=scroll_and_check(page,self.log,self.stop_event,timeout=150)
                                count=save_mhtml(page,target,info,self.log)
                            record_checkpoint(folder,num,info['valid'],count,item['url'])
                            saved.append(item)
                            total_success+=1
                            write_index(folder,saved,title)
                        except InterruptedError:raise
                        except Exception as exc:
                            failed.append(f'Chap {num}: {exc}')
                            self.log(f'Chap {num}: KHÔNG LƯU – {exc}')
                        self.emit('progress',idx,len(chapters),f'{title}: đã xử lý {idx}/{len(chapters)}')
                    lines=[f'TruyenSaver v{VERSION}',f'Truyện: {title}',f'Nguồn: {url}',
                        f'Đã lưu hoặc bỏ qua: {len(saved)}/{len(chapters)}','',
                        'LỖI / CHƯƠNG CẦN KIỂM TRA:']+failed
                    (folder/'BAO_CAO.txt').write_text('\n'.join(lines),encoding='utf-8')
                    write_index(folder,saved,title)
                    results.append(f'{title}: {len(saved)}/{len(chapters)} chap')
                    self.log(f'{title}: xong {len(saved)}/{len(chapters)}. Mở MUC_LUC.html để xem offline.')
            finally:page.close()
        return 'Hoàn thành:\n'+'\n'.join(results)+'\nHãy thử ngắt mạng và mở MHTML để kiểm tra.'

    def on_close(self):
        if self.busy:
            self.stop_event.set()
            messagebox.showinfo('Đang dừng','Đã yêu cầu dừng. Hãy đóng ứng dụng sau khi tiến trình kết thúc.')
            return
        self.save_settings()
        try:self.browser.shutdown()
        except Exception:pass
        self.root.destroy()


if __name__ == '__main__':
    root=tk.Tk()
    try:ttk.Style().theme_use('clam')
    except tk.TclError:pass
    App(root)
    root.mainloop()
