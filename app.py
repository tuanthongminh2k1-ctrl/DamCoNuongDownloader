"""TruyenSaver Chromium Offline v3.1 – portable Windows application.

This app uses CDP to save MHTML, NOT Ctrl+S HTML+_files.
No Python is required on the Windows machine *after* GitHub builds the EXE.
"""
from __future__ import annotations

"""Core routines for offline chapter archival. No UI or browser dependency on import.

MHTML captures are intentionally verified against reader-image URLs, not by the
number of embedded images (which may include advertisements/icons).
"""

import html
import json
import os
import re
import time
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

VERSION = '4.0.0'
PDF_RENDER_VERSION = 'reader-width-balanced-v4'
SUPPORTED_HOSTS = ('damconuong.cool', 'sayhentai.cx', 'hentaicube.xyz')
CHAPTER_RE = re.compile(r'(?:chap(?:ter)?|chương|chuong)\s*[:#\-_. ]*\s*(\d+(?:[.,]\d+)?)', re.I)


def sanitize(text: str, fallback='Truyen', limit=90) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(text)).strip(' .')
    return (value[:limit].rstrip(' .') or fallback)


def chapter_label(number: float) -> str:
    number = float(number)
    return f'{int(number):03d}' if number.is_integer() else f'{number:.3f}'.rstrip('0').rstrip('.')


def normalize_url(value: str) -> str:
    if not value:
        return ''
    value = html.unescape(value).strip().strip('<>')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https'):
        return value
    host = (parsed.hostname or '').lower()
    port = f':{parsed.port}' if parsed.port and parsed.port not in (80, 443) else ''
    # Preserve case-sensitive path and query. Fragment is never an image resource identifier.
    return urlunsplit((parsed.scheme.lower(), host + port, unquote(parsed.path), parsed.query, ''))


def validate_source(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or '').lower().removeprefix('www.')
    if p.scheme != 'https' or host not in SUPPORTED_HOSTS or p.username or p.password:
        raise ValueError('Chỉ nhận HTTPS: damconuong.cool, sayhentai.cx, hentaicube.xyz.')
    return url


def chapter_from_anchor(text: str, link: str, series_url: str) -> float | None:
    ref, dest = urlsplit(series_url), urlsplit(urljoin(series_url, link))
    host = (ref.hostname or '').lower().removeprefix('www.')
    if host != (dest.hostname or '').lower().removeprefix('www.'):
        return None
    series_path = ref.path.rstrip('/').lower()
    candidate_path = dest.path.rstrip('/').lower()
    stem = series_path.rsplit('/', 1)[-1].removesuffix('.html')
    # Three sites use different chapter-link formats. Require the SAME series
    # prefix, so a link to another comic can never leak into this chapter list.
    base = series_path.removesuffix('.html')
    valid = (candidate_path.startswith(series_path + '/') or
             candidate_path.startswith(base + '/') or
             candidate_path.startswith(base + '-chapter-') or
             candidate_path.startswith(base + '-chap-') or
             candidate_path.startswith(base + '-chuong-'))
    # Some hosts append "-chap-1.html" to the end of the series slug.
    if valid and not re.search(r'(?:chapter|chap|chuong)[/_ -]*\d', candidate_path, re.I):
        valid = False
    if not valid:
        return None
    # Prefer visible chapter text: it preserves decimals such as 1.5 even
    # when the URL spells the chapter as chapter-1-5.
    found = CHAPTER_RE.findall(text or '')
    if not found:
        found = CHAPTER_RE.findall(candidate_path.replace('-', ' ').replace('_', ' '))
    if not found:
        return None
    try:
        return float(found[-1].replace(',', '.'))
    except ValueError:
        return None


def extract_chapters(anchors: list[dict], url: str) -> list[dict]:
    found = {}
    for item in anchors:
        link = urljoin(url, item.get('href') or '')
        number = chapter_from_anchor(item.get('text') or '', link, url)
        if number is not None and number not in found:
            found[number] = {'number': number, 'url': link}
    return [found[n] for n in sorted(found)]


@dataclass
class ArchiveCheck:
    ok: bool
    matched: int
    expected: int
    extra_images: int
    explanation: str
    missing_urls: list[str]


def _mhtml_resources(blob: bytes) -> tuple[set[str], int, list[str]]:
    msg = BytesParser(policy=policy.default).parsebytes(blob)
    if not msg.is_multipart() or msg.get_content_type() != 'multipart/related':
        raise ValueError('Tệp không phải MHTML multipart/related')
    sources: set[str] = set()
    image_parts = 0
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_maintype() == 'image':
            image_parts += 1
            loc = part.get('Content-Location')
            if loc:
                sources.add(normalize_url(str(loc)))
        if part.get_content_type() == 'text/html':
            payload = part.get_payload(decode=True) or b''
            html_parts.append(payload.decode(part.get_content_charset() or 'utf-8', errors='replace'))
    return sources, image_parts, html_parts


def verify_mhtml(blob: bytes, reader_urls: list[str]) -> ArchiveCheck:
    """Require every observed remote reader image to be embedded.

    Conservative by design: if an image isn't present, mark chapter for retry.
    An archive can still be incomplete if the website never exposed an image.
    """
    expected = list(dict.fromkeys(normalize_url(url) for url in reader_urls if url))
    if not expected:
        return ArchiveCheck(False, 0, 0, 0, 'Không phát hiện ảnh truyện', [])
    if len(blob) < 1500:
        return ArchiveCheck(False, 0, len(expected), 0, 'Tệp lưu quá nhỏ', expected)
    try:
        resources, extras, html_parts = _mhtml_resources(blob)
    except (ValueError, TypeError, UnicodeError, Exception) as ex:
        return ArchiveCheck(False, 0, len(expected), 0, f'MHTML không đọc được: {ex}', expected)
    text = '\n'.join(html_parts)
    missing = []
    matched = 0
    for url in expected:
        if url.startswith('data:image/'):
            # Inline assets can remain in HTML as data: URI, not separate image MIME parts.
            # Check the URI itself only if it is short enough for the HTML decoder.
            if url in text or (len(url) > 128 and url[:100] in text):
                matched += 1
            else:
                missing.append(url[:180])
        elif url.startswith('http://') or url.startswith('https://'):
            if url in resources:
                matched += 1
            else:
                missing.append(url)
        else:
            # blob: and non-HTTP resources are not guaranteed to survive offline.
            missing.append(url[:180])
    reason = ('Tất cả ảnh đã quan sát đều có trong MHTML (chưa xác minh nội dung bị ẩn)'
              if not missing else f'Thiếu {len(missing)}/{len(expected)} tài nguyên ảnh truyện')
    return ArchiveCheck(not missing, matched, len(expected), extras, reason, missing)


def manifest_path(folder: Path) -> Path:
    return folder / 'TIEN_DO_LUU.json'


def read_manifest(folder: Path) -> dict:
    try:
        obj = json.loads(manifest_path(folder).read_text(encoding='utf-8'))
        return obj if isinstance(obj, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


MANIFEST_LOCK = __import__('threading').RLock()

def write_manifest_entry(folder: Path, chapter: str, url: str, file: Path, expected: int, matched: int,
                         reader_urls: list[str] | None = None, ads_filtered: bool = False):
    with MANIFEST_LOCK:
        folder.mkdir(parents=True, exist_ok=True)
        data = read_manifest(folder)
        data[chapter] = {'url': url, 'file': file.name, 'size': file.stat().st_size,
                         'version': VERSION, 'expected': expected, 'matched': matched,
                         'reader_urls': reader_urls or [], 'ads_filtered': bool(ads_filtered),
                         'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')}
        tmp = manifest_path(folder).with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, manifest_path(folder))


def completed(folder: Path, chapter: str, url: str, file: Path, require_ad_free: bool = False) -> bool:
    entry = read_manifest(folder).get(chapter)
    return (isinstance(entry, dict) and entry.get('url') == url
            and entry.get('version') in ('2.0.0', '2.1.0', '2.2.0', '2.3.0', '2.4.0', '2.5.0', '3.0.0', '3.1.0', VERSION) and entry.get('file') == file.name
            and (not require_ad_free or bool(entry.get('ads_filtered', False)))
            and entry.get('expected', 0) > 0
            and entry.get('expected') == entry.get('matched')
            and file.is_file() and file.stat().st_size > 1500
            and file.stat().st_size == entry.get('size'))


def write_index(folder: Path, series: str, chapters: list[dict]) -> None:
    saved = []
    for item in chapters:
        n = chapter_label(item['number'])
        filename = f'Chap_{n}.mhtml'
        if completed(folder, n, item['url'], folder / filename):
            saved.append((n, filename))
    elements = ''.join(f'<li><a href="{html.escape(fn, quote=True)}">Chap {html.escape(n)}</a></li>'
                       for n, fn in saved)
    page = ('<!doctype html><html lang="vi"><meta charset="utf-8">'
            '<title>Mục lục offline</title><style>body{font:18px Segoe UI,Arial;max-width:760px;'
            'margin:3rem auto;padding:1rem}li{margin:.7rem 0}a{color:#135cad}</style>'
            f'<h1>{html.escape(series)}</h1><p>Đã xác minh tài nguyên ảnh trong tệp: {len(saved)} chap. '
            'Nên mở thử khi ngắt mạng.</p><ol>' + elements + '</ol></html>')
    path = folder / 'MUC_LUC.html'
    path.write_text(page, encoding='utf-8')


# Browser-side reader analysis. Do not use the WHOLE DOCUMENT bottom as the
# condition for reader completion. Ad slots, comments and infinite-scroll widgets
# can change the page height indefinitely even when all 35/35 comic pages are ready.
READER_JS = r'''() => {
 const selectors=['.reading-detail','.chapter-content','.page-chapter','.viewer-content',
 '.reading-content','#chapter-content','#chapter-reading-content','.container-chapter-reader',
 '.chapter-images','.reading__content','.wp-manga-chapter-img','#reader','#readerarea',
 '.chapter-content-wrap','#chapter-body','.chapter-reader','.read-container',
 '#image-container','.entry-content','main'];
 const bad='[class*="advert"],[id*="advert"],.adsbygoogle,.sidebar,.banner,.ads,nav,footer,header';
 let choices=[];
 for (const [index,sel] of selectors.entries()) {
  for (const el of document.querySelectorAll(sel)) {
   let imgs=[...el.querySelectorAll('img')].filter(i=>!i.closest(bad));
   if (imgs.length) choices.push({sel,el,index,imgs});
  }
 }
 choices.sort((a,b)=> a.index-b.index || b.imgs.length-a.imgs.length);
 let selected=choices[0];
 if (!selected) return {selector:'',total:0,ready:0,missing:0,valid:0,
      images:[],readerEnd:false,scrollY,viewport:innerHeight};
 let images=selected.imgs.map((i,idx)=>{
   if(!i.dataset.truyensaverId) i.dataset.truyensaverId='ts-'+idx;
   let current=i.currentSrc||i.src||'';
   let deferred=i.getAttribute('data-src')||i.getAttribute('data-original')||i.getAttribute('data-lazy-src')||'';
   const placeholder=(!current || current.startsWith('data:image/svg') ||
     /(?:placeholder|loading|spacer|lazy\.gif)/i.test(current));
   let desired=(placeholder&&deferred)?new URL(deferred,location.href).href:current;
   const ready=Boolean(i.complete&&i.naturalWidth>=32&&i.naturalHeight>=32&&!placeholder);
   return {key:i.dataset.truyensaverId,src:desired,ready,width:i.naturalWidth||0,
     height:i.naturalHeight||0,pending:placeholder,top:Math.round(i.getBoundingClientRect().top+scrollY)};
 }).filter(i=>i.src && (i.width>=16||i.height>=16||!i.ready) &&
    !/\/(?:banner|advert|ads?)[\/_-]|(?:logo|avatar|tracking|pixel)\.(?:jpg|png|gif|webp)/i.test(i.src));
 // Reader-tail means the end of the LAST IMAGE, not the end of the entire page.
 // For long-strip manga, the last image can be much taller than the viewport.
 const chosenKeys=new Set(images.map(x=>x.key));
 let last=null;
 for(const element of selected.imgs) {
   if(chosenKeys.has(element.dataset.truyensaverId)) last=element;
 }
 const lastBottom=last?last.getBoundingClientRect().bottom:selected.el.getBoundingClientRect().bottom;
 const top=last?last.getBoundingClientRect().top:0;
 const scrollRoot=document.scrollingElement||document.documentElement;
 const canScrollFurther=(scrollRoot.scrollTop+innerHeight < scrollRoot.scrollHeight-15);
 const readerEnd=Boolean(last && top <= innerHeight+100 && lastBottom >= -100);
 return {selector:selected.sel,total:images.length,ready:images.filter(i=>i.ready).length,
 missing:images.filter(i=>!i.ready).length,valid:images.filter(i=>i.ready).length,
 images,readerEnd,scrollY,viewport:innerHeight,readerTail:Math.round(lastBottom),
 documentHeight:scrollRoot.scrollHeight};
}'''

# Trigger in-page loading without synthesizing keyboard/mouse input.
EAGER_JS = r'''() => {
 for(const i of document.querySelectorAll('img')) {
   if(i.loading==='lazy') i.loading='eager';
   const v=i.getAttribute('data-src')||i.getAttribute('data-original')||i.getAttribute('data-lazy-src');
   const current=i.getAttribute('src')||'';
   if(v && (!current||current.startsWith('data:image/svg')||/placeholder|loading|spacer|lazy\.gif/i.test(current))) {
     try { i.src=new URL(v,location.href).href; } catch(e) {}
   }
 }
}'''


def reader_signature(info: dict) -> tuple:
    '''Only image identities and their readiness matter for settling the reader.

    Do NOT include documentHeight. A moving advertisement below the chapter must
    never prevent a 35/35 successfully-loaded reader from completing.
    '''
    return (tuple((x['key'], normalize_url(x['src']), bool(x['ready']))
                  for x in info.get('images', [])))


def ready_to_archive(info: dict, seen_reader_tail: bool, stable_seconds: float) -> bool:
    '''Pure, testable completion gate: all discovered comic images and reader tail.'''
    return bool(info.get('selector') and info.get('total', 0) > 0
                and info.get('missing', 1) == 0
                and seen_reader_tail and stable_seconds >= 3.0)


def _scroll_to_last_reader_image(page) -> bool:
    """Visit the last *reader image*, not the bottom of the whole document.

    Return True only when we found a real last image in the selected reader.
    The img may be taller than the viewport or live in a nested scroll root.
    """
    return bool(page.evaluate("""() => {
      const nodes=[...document.querySelectorAll('img[data-truyensaver-id]')];
      const last=nodes[nodes.length-1];
      if (!last || !last.isConnected) return false;
      last.scrollIntoView({block:'end',inline:'nearest',behavior:'instant'});
      return true;
    }"""))


def wait_for_reader(page, log, stop, max_seconds=155) -> dict:
    """Load and verify the reader's images without requiring page-bottom.

    In particular, 35/35 loaded must not be rejected merely because an
    infinitely extending ad/footer prevents `readerEnd` from being true.
    We *do* require a stable list and explicit visit to the last reader image,
    then verify the actual image resources in the captured MHTML separately.
    """
    deadline = time.monotonic() + max_seconds
    info = {}
    try:
        page.evaluate('window.scrollTo(0,0)')
        page.evaluate(EAGER_JS)
    except Exception:
        if stop.is_set():
            raise InterruptedError('Đã dừng')
        raise
    last_signature = None
    stable_since = time.monotonic()
    last_y = None
    stuck_steps = 0
    visited_last = False
    last_probe_at = 0.0
    count = 0
    while time.monotonic() < deadline and count < 1300:
        if stop.is_set():
            raise InterruptedError('Đã dừng')
        try:
            info = page.evaluate(READER_JS)
        except Exception:
            if stop.is_set():
                raise InterruptedError('Đã dừng')
            raise
        count += 1
        if not info.get('selector') or not info.get('total'):
            if count > 30:
                raise RuntimeError('Không tìm thấy ảnh truyện. Website có thể yêu cầu đăng nhập.')
            page.wait_for_timeout(200)
            continue
        sig = reader_signature(info)
        now = time.monotonic()
        if sig != last_signature:
            stable_since = now
            last_signature = sig
        stable = now - stable_since
        loaded = info.get('missing', 1) == 0 and info.get('ready', 0) == info.get('total', 0)
        # When all observed images are ready, explicitly visit the last image.
        # This works even if the last image is longer than a screen and a sticky
        # footer or a nested scroller makes a viewport-only tail flag unreliable.
        if loaded and now-last_probe_at >= 0.8:
            if stop.is_set():
                raise InterruptedError('Đã dừng')
            try:
                visited_last |= _scroll_to_last_reader_image(page)
            except Exception:
                if stop.is_set():
                    raise InterruptedError('Đã dừng')
                raise
            last_probe_at = now
        tail_seen = bool(info.get('readerEnd')) or visited_last
        if ready_to_archive(info, tail_seen, stable):
            if stop.is_set():
                raise InterruptedError('Đã dừng')
            # Make the independent confirmation after a short settle delay.
            page.wait_for_timeout(350)
            if stop.is_set():
                raise InterruptedError('Đã dừng')
            confirm = page.evaluate(READER_JS)
            if (confirm.get('missing') == 0 and
                    confirm.get('ready') == confirm.get('total') and
                    reader_signature(confirm) == sig):
                urls = [normalize_url(x['src']) for x in confirm['images']]  # Keep repeated panels in their original order.
                if urls:
                    log(f"    Ảnh truyện đã tải ổn định: {confirm['ready']}/{confirm['total']}; "
                        f"đã kiểm tra ảnh cuối vùng đọc (không phụ thuộc chân trang web).")
                    return {'urls': urls, 'total': len(urls), 'selector': confirm['selector']}
            # If the list changed while confirming, restart stabilization.
            last_signature = None
            stable_since = time.monotonic()
            continue
        if stop.is_set():
            raise InterruptedError('Đã dừng')
        if loaded and visited_last:
            page.wait_for_timeout(190)
            continue
        # For lazy images, sweep down the page until the reader tail is seen.
        # Once apparently stuck, activate missing images individually instead
        # of scrolling forever through unrelated comments and advertisements.
        y=info.get('scrollY')
        if y == last_y:
            stuck_steps += 1
        else:
            stuck_steps = 0
        last_y = y
        if stuck_steps >= 3:
            incomplete = next((i for i in info.get('images',[]) if not i['ready']), None)
            if incomplete:
                page.evaluate("""key => {
                   const el=[...document.querySelectorAll('img')].find(i=>i.dataset.truyensaverId===key);
                   if(el) {el.loading='eager';el.scrollIntoView({block:'center',behavior:'instant'});}
                }""",incomplete['key'])
            else:
                visited_last |= _scroll_to_last_reader_image(page)
            page.evaluate(EAGER_JS)
            stuck_steps = 0
            page.wait_for_timeout(250)
        else:
            page.evaluate('window.scrollBy(0,Math.max(540,Math.floor(innerHeight*0.96)))')
            page.wait_for_timeout(120)
    if stop.is_set():
        raise InterruptedError('Đã dừng')
    explanation = (f"{info.get('ready',0)}/{info.get('total',0)} ảnh; "
                   f"ảnh cuối đã kiểm tra: {'có' if visited_last else 'chưa'}")
    raise RuntimeError('Không thể xác nhận đủ ảnh truyện sau khi chờ: '+explanation+
                       '. Không tự lưu thiếu; kiểm tra đăng nhập hoặc ảnh lỗi.')


# ---- Windows UI and browser controller ----

"""TruyenSaver Chromium Offline 3.1 (Windows).

Runs a bundled Chromium using a private persistent profile through CDP.
No desktop keyboard or mouse simulation, no Chromium installation required.
Stores MHTML snapshots rather than Ctrl+S HTML plus _files.
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

APP_NAME = 'TruyenSaver Chromium Offline'
CONFIG_DIR = Path(os.getenv('APPDATA') or Path.home()) / 'TruyenSaverChromium'
PROFILE_DIR = CONFIG_DIR / 'Profile_v2_3'
SETTINGS = CONFIG_DIR / 'settings_v4.json'
PREVIOUS_SETTINGS = CONFIG_DIR / 'settings_v3.json'
LEGACY_SETTINGS = Path(os.getenv('APPDATA') or Path.home()) / 'TruyenSaverCocCoc' / 'settings_v2.json'


def asset_path(name: str) -> Path:
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return root / 'assets' / name


class HostHealth:
    """Thread-safe, per-host retry cooldown. Never intentionally bypass access controls."""
    def __init__(self):
        self._lock=threading.RLock()
        self._failures={}
        self._until={}

    def wait(self, url, stop):
        host=urlsplit(url).hostname or ''
        while True:
            if stop.is_set():
                raise InterruptedError('Đã dừng trong khi chờ phục hồi mạng')
            with self._lock:
                remaining=self._until.get(host,0)-time.monotonic()
            if remaining<=0:
                return
            stop.wait(min(0.3,remaining))

    def failed(self,url,kind='network'):
        host=urlsplit(url).hostname or ''
        with self._lock:
            n=min(5,self._failures.get(host,0)+1)
            self._failures[host]=n
            delay=min(28., 1.75*(2**(n-1))) if kind!='forbidden' else 0.
            self._until[host]=max(self._until.get(host,0),time.monotonic()+delay)
            return delay

    def succeeded(self,url):
        host=urlsplit(url).hostname or ''
        with self._lock:
            self._failures[host]=0
            self._until[host]=0

    def reduced_concurrency(self,url):
        with self._lock:
            return self._failures.get(urlsplit(url).hostname or '',0)>=2


def configure_chromium_path():
    """Point Playwright at the browser included next to the portable EXE."""
    frozen_root = getattr(sys, '_MEIPASS', None)
    if frozen_root:
        embedded = Path(frozen_root) / 'playwright-browsers'
        if not embedded.is_dir():
            raise RuntimeError('Bộ Chromium đi kèm bị thiếu. Hãy giải nén TOÀN BỘ thư mục ZIP, '
                               'không di chuyển riêng file EXE.')
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(embedded)


def chromium_executable():
    configure_chromium_path()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as driver:
            candidate = Path(driver.chromium.executable_path)
    except Exception as exc:
        raise RuntimeError('Không thể xác định Chromium đi kèm: ' + str(exc)) from exc
    if not candidate.is_file() and not getattr(sys, 'frozen', False):
        # Development/testing environments may provide system Chromium instead.
        import shutil
        fallback = shutil.which('chromium') or shutil.which('chromium-browser')
        if fallback:
            candidate = Path(fallback)
    if not candidate.is_file():
        raise RuntimeError('Không tìm thấy Chromium tại ' + str(candidate) + '. '
                           'Hãy giải nén lại toàn bộ bản Windows từ GitHub Artifacts.')
    return str(candidate)


def get_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def is_ready(port: int) -> bool:
    try:
        with urlopen(f'http://127.0.0.1:{port}/json/version', timeout=0.65) as conn:
            return conn.status == 200
    except Exception:
        return False


@dataclass
class ChromiumEngine:
    port: int | None = None
    process: subprocess.Popen | None = None
    mode: str = ''

    def close(self):
        if self.port and is_ready(self.port):
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as driver:
                    browser = driver.chromium.connect_over_cdp(f'http://127.0.0.1:{self.port}', timeout=5000)
                    browser.new_browser_cdp_session().send('Browser.close')
            except Exception:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
            for _ in range(25):
                if not is_ready(self.port):
                    break
                time.sleep(0.18)
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=4)
            except Exception:
                pass
        self.port = None
        self.process = None
        self.mode = ''

    def abort(self):
        """Emergency stop: end every active network request in our Chromium.

        This does not send keystrokes, does not affect other browser windows,
        and is called from a worker so the GUI remains responsive.
        """
        proc=self.process
        self.port=None
        self.process=None
        self.mode=''
        if not proc or proc.poll() is not None:
            return
        if sys.platform == 'win32':
            try:
                subprocess.run(['taskkill','/F','/T','/PID',str(proc.pid)],
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                               creationflags=0x08000000,timeout=8,check=False)
            except Exception:
                pass
        if proc.poll() is None:
            try: proc.kill()
            except OSError: pass

    def ensure(self, *, visible=False) -> int:
        requested = 'visible' if visible else 'headless'
        if self.port and is_ready(self.port) and self.mode == requested:
            return self.port
        if self.port:
            self.close()
        executable = chromium_executable()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.port = get_free_port()
        self.mode = requested
        args = [executable, '--no-first-run', '--no-default-browser-check',
                '--remote-debugging-address=127.0.0.1',
                f'--remote-debugging-port={self.port}', f'--user-data-dir={PROFILE_DIR}',
                '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
                '--disable-backgrounding-occluded-windows']
        if not visible:
            args.extend(['--headless=new', '--window-size=1366,1024'])
        if sys.platform != 'win32' and hasattr(os, 'geteuid') and os.geteuid() == 0:
            args.append('--no-sandbox')  # Only for local root-run Linux tests.
        args.append('about:blank')
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=(0x08000000 if sys.platform == 'win32' else 0))
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if is_ready(self.port):
                return self.port
            if self.process.poll() is not None:
                self.close()
                raise RuntimeError('Chromium không mở được. Kiểm tra đủ thư mục _internal, '
                                   'phần mềm diệt virus và dung lượng ổ đĩa.')
            time.sleep(0.3)
        self.close()
        raise RuntimeError('Chromium đi kèm không bật được chế độ điều khiển chạy nền.')


# Remove advertisement nodes from rendered page *after* image URLs were determined.
# Conservatively retain all ancestors of the verified comic images, and preserve
# the actual pixels of the comic artwork. No adblock network rules are applied:
# blocking requests can break legitimate comic images on third-party CDNs.
REMOVE_ADS_JS = r'''(expectedUrls) => {
  function normalizeImageUrl(value) {
    if (!value) return '';
    try {
      const parsed = new URL(value, location.href);
      if (parsed.protocol === 'data:') return value;
      parsed.hash = '';
      let path = parsed.pathname;
      try { path = decodeURIComponent(path); } catch (_) {}
      return parsed.origin + path + parsed.search;
    } catch (_) { return value; }
  }
  const allow = new Set((expectedUrls || []).map(normalizeImageUrl));
  const protectedNodes = new Set();
  function isComicImage(img) {
    return [img.currentSrc, img.src, img.getAttribute('data-src'),
            img.getAttribute('data-original'), img.getAttribute('data-lazy-src')]
           .some(value => allow.has(normalizeImageUrl(value)));
  }
  for (const img of document.querySelectorAll('img')) {
    if (!isComicImage(img)) continue;
    let node = img;
    while (node) { protectedNodes.add(node); node = node.parentElement; }
  }
  const adsClass = /(?:^|[-_\s])(?:ad|ads|adslot|adunit|adbox|advert|advertisement|advertising|banner|sponsor|sponsored|popunder|popup)(?:$|[-_\s0-9])/i;
  const adsUrl = /(?:doubleclick\.net|googlesyndication\.com|googleadservices\.com|adsystem|adservice|\/(?:ads?|banner|advertisement)(?:\/|\?|\.|-|_))/i;
  const safeRemove = (node) => {
    if (!node || protectedNodes.has(node) || !node.parentNode ||
        node === document.body || node === document.documentElement) return false;
    node.remove(); return true;
  };
  // First remove declared ad slots, ad iframes, and labeled overlay containers.
  const candidates = [...document.querySelectorAll('body *')];
  let removed = 0, floating = 0;
  for (const node of candidates) {
    if (!node.isConnected || protectedNodes.has(node)) continue;
    const tag = node.tagName.toLowerCase();
    const label = [node.id || '', typeof node.className === 'string' ? node.className : '',
                   node.getAttribute('aria-label') || ''].join(' ');
    const src = node.getAttribute('src') || '';
    const href = node.getAttribute('href') || '';
    const tagged = adsClass.test(label) || (tag === 'iframe' && adsUrl.test(src));
    const namedAdResource = (tag === 'img' || tag === 'iframe') && adsUrl.test(src);
    // Do not remove any generic element just because it contains an ad link;
    // this prevents stripping whole comic navigation/reader containers.
    if ((tagged || namedAdResource) && safeRemove(node)) removed++;
  }
  // A floating ad may have an arbitrary class and an image/banner as content.
  // Restrict this rule to fixed/sticky small docked panels, outside reader images.
  for (const node of [...document.querySelectorAll('body *')]) {
    if (!node.isConnected || protectedNodes.has(node)) continue;
    if (!node.querySelector('img,iframe') && !['IMG','IFRAME'].includes(node.tagName)) continue;
    const style = getComputedStyle(node);
    if (style.position !== 'fixed' && style.position !== 'sticky') continue;
    const rect = node.getBoundingClientRect();
    if (rect.width < 140 || rect.height < 28 || rect.height > innerHeight * 0.42 ||
        rect.top > innerHeight || rect.bottom < innerHeight - 175) continue;
    // Ignore extremely large wrappers, which can be modal comic viewers.
    if (rect.width * rect.height > innerWidth * innerHeight * .50) continue;
    if (safeRemove(node)) floating++;
  }
  return {removed, floating, protected: protectedNodes.size};
}'''


def capture_verified_blob(page, expected: dict, log, strip_ads: bool = True) -> tuple[bytes, ArchiveCheck]:
    """Take a CDP snapshot and verify its exact reader-image resources before writing."""
    if strip_ads:
        try:
            cleaned = page.evaluate(REMOVE_ADS_JS, expected['urls'])
            log('    Đã lọc quảng cáo trước khi lưu: '
                f"{cleaned['removed']} ô quảng cáo, {cleaned['floating']} banner nổi.")
        except Exception as exc:
            # Explicitly refuse a supposedly ad-free snapshot if DOM cleaning fails.
            raise RuntimeError('Không áp dụng được bộ lọc quảng cáo: ' + str(exc)) from exc
    session = page.context.new_cdp_session(page)
    try:
        data = session.send('Page.captureSnapshot', {'format': 'mhtml'})['data'].encode('utf-8')
    finally:
        try:
            session.detach()
        except Exception:
            pass
    check = verify_mhtml(data, expected['urls'])
    if not check.ok:
        log(f'    MHTML thiếu ảnh: {check.matched}/{check.expected}.')
        for missing in check.missing_urls[:3]:
            log(f'    Ảnh chưa có: {missing[:160]}')
        raise RuntimeError(check.explanation)
    return data, check


def save_atomic_bytes(data: bytes, output: Path, stop=None) -> None:
    if stop is not None and stop.is_set():
        raise InterruptedError('Đã dừng trước khi ghi MHTML')
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + '.part')
    try:
        tmp.write_bytes(data)
        if stop is not None and stop.is_set():
            raise InterruptedError('Đã dừng trước khi lưu MHTML')
        os.replace(tmp, output)
    finally:
        tmp.unlink(missing_ok=True)


def offline_reader_urls(blob: bytes, chapter_url: str, expected_count: int) -> list[str]:
    """Recover URL order from an *existing* MHTML saved by an older release.

    Only accept a reader container with an image count matching the original
    manifest, all of which must already exist in the MHTML snapshot. Refuse
    uncertain matches rather than quietly exporting a partial chapter.
    """
    from html.parser import HTMLParser
    resources, _, html_parts = _mhtml_resources(blob)
    priorities = ('reading-detail', 'chapter-content', 'page-chapter',
                  'viewer-content', 'reading-content', 'chapter-reading-content',
                  'container-chapter-reader', 'chapter-images', 'reader',
                  'readerarea', 'chapter-body', 'entry-content', 'main')
    # Ignore images in an advertising container even if nested in the reader.
    exclude = ('advert', 'banner', 'adsbygoogle', 'sidebar', 'logo', 'avatar')

    class ReaderParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.opened = []
            self.found = {p: [] for p in priorities}

        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            classes = set((d.get('class') or '').lower().split())
            id_value = (d.get('id') or '').lower()
            names = classes | {id_value}
            active = set()
            if self.opened:
                active = self.opened[-1][1].copy()
            for name in names:
                if name in priorities:
                    active.add(name)
            bad = any(any(word in name for word in exclude) for name in names)
            if self.opened and self.opened[-1][2]:
                bad = True
            if tag.lower() == 'img' and active and not bad:
                raw = (d.get('data-src') or d.get('data-original') or d.get('src') or '')
                if raw:
                    uri = normalize_url(urljoin(chapter_url, raw))
                    if (uri in resources or uri.startswith('data:image/')):
                        for selector in active:
                            self.found[selector].append(uri)
            if tag.lower() not in ('img', 'meta', 'link', 'br', 'hr', 'input', 'source'):
                self.opened.append((tag.lower(), active, bad))

        def handle_endtag(self, tag):
            # Close the nearest matching element even if the webpage HTML is malformed.
            for i in range(len(self.opened)-1, -1, -1):
                if self.opened[i][0] == tag.lower():
                    del self.opened[i:]
                    break

    parser = ReaderParser()
    for html_part in html_parts:
        parser.feed(html_part)
    for key in priorities:
        urls = list(dict.fromkeys(parser.found[key]))
        if (len(urls) == expected_count and
                verify_mhtml(blob, urls).ok):
            return urls
    return []


def _mhtml_images(blob: bytes) -> dict[str, bytes]:
    """Get only encoded image resources from the validated MHTML, keyed by Content-Location."""
    msg = BytesParser(policy=policy.default).parsebytes(blob)
    if not msg.is_multipart():
        raise ValueError('MHTML không có tài nguyên ảnh.')
    mapping: dict[str, bytes] = {}
    for part in msg.walk():
        if part.is_multipart() or part.get_content_maintype() != 'image':
            continue
        location = part.get('Content-Location')
        body = part.get_payload(decode=True)
        if location and body:
            mapping[normalize_url(str(location))] = body
    return mapping


def _pdf_image_reader(resource: bytes):
    """Return a ReportLab image with accurate pixels and minimal re-encoding.

    JPEG and PNG remain in their source encodings; WebP and other formats are
    decoded, with transparent artwork composited onto white, then encoded to
    high-quality JPEG. No image is cropped, sliced or discarded.
    """
    import io
    from PIL import Image, ImageOps
    from reportlab.lib.utils import ImageReader

    stream = io.BytesIO(resource)
    with Image.open(stream) as opened:
        opened.verify()  # Detect truncated and corrupt files before writing PDF.
    stream.seek(0)
    with Image.open(stream) as opened:
        opened.load()
        exif_orientation = opened.getexif().get(274, 1)
        if opened.format in ('JPEG', 'PNG') and exif_orientation in (None, 1):
            # Preserve original JPEG bytes and lossless PNG pixels without re-encoding.
            return ImageReader(io.BytesIO(resource)), opened.width, opened.height
        picture = ImageOps.exif_transpose(opened)
        if picture.width <= 0 or picture.height <= 0:
            raise ValueError('Ảnh truyện không có kích thước hợp lệ')
        width, height = picture.size
        # All other formats: avoid losing detail through default low-quality JPEG.
        if picture.mode in ('RGBA', 'LA') or (picture.mode == 'P' and 'transparency' in picture.info):
            rgba = picture.convert('RGBA')
            background = Image.new('RGB', rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel('A'))
            picture = background
        else:
            picture = picture.convert('RGB')
        encoded = io.BytesIO()
        picture.save(encoded, format='JPEG', quality=96, subsampling=0, optimize=True)
        encoded.seek(0)
        return ImageReader(encoded), width, height


def _pdf_probe(resource: bytes) -> tuple[int, int]:
    """Validate header/content without permanently caching decoded pixel buffers."""
    import io
    from PIL import Image
    with Image.open(io.BytesIO(resource)) as picture:
        size = picture.size
        picture.verify()
    if size[0] <= 0 or size[1] <= 0:
        raise ValueError('Ảnh truyện không có kích thước hợp lệ')
    return size


def plan_continuous_pdf(sizes: list[tuple[int, int]], target_width=720.,
                        max_height=2500., min_width=240.) -> list[tuple[float,float,list[int]]]:
    """Keep readable pages, with breaks only *between* source images.

    An extremely tall single input is handled separately by pdf_from_mhtml,
    because shrinking it to <240 pt recreates the user's illegibly thin PDFs.
    """
    if not sizes or any(w <= 0 or h <= 0 for w,h in sizes):
        raise ValueError('Kích thước ảnh không hợp lệ')
    out=[]; group=[]; sumratio=0.
    for idx,(w,h) in enumerate(sizes):
        ratio=h/w
        if ratio*target_width > max_height+0.1:
            raise ValueError('Ảnh đơn quá dài: hãy chia ảnh thành các mảnh trước khi lập trang PDF')
        if group and (sumratio+ratio)*target_width>max_height:
            out.append((target_width,target_width*sumratio,group))
            group=[]; sumratio=0.
        group.append(idx); sumratio+=ratio
        if sumratio*target_width>max_height:
            # Caller slices excessive single sources into overlapped chunks.
            if len(group)>1:
                raise ValueError('Cần phân nhóm ảnh theo giới hạn trang')
            out.append((target_width,target_width*sumratio,group))
            group=[];sumratio=0.
    if group:
        out.append((target_width,target_width*sumratio,group))
    return out


def _source_slices(raw: bytes, max_ratio=3.45):
    """Split only an exceptionally tall *single image*; preserve every source row.

    Prefer near-white horizontal rows for natural breaks. No row is discarded.
    Extremely tall scans cannot be one readable PDF page in all viewers.
    """
    import io
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as source:
        source.load()
        w,h=source.size
        max_px=max(1,int(w*max_ratio))
        if h<=max_px:
            return [raw]
        pieces=[]; top=0
        while top<h:
            target=min(top+max_px,h)
            if target<h:
                # Low-information horizontal scanlines make better page breaks.
                # Search only 8% around target, avoid costly full image scans.
                search_start=max(top+max_px//2,target-max_px//15)
                search_end=min(h-1,target+max_px//15)
                best=(float('inf'),target)
                sample_step=max(1,w//120)
                for y in range(search_start,search_end, max(1,(search_end-search_start)//28)):
                    band=source.crop((0,y,w,min(h,y+2))).convert('RGB')
                    colors=list(band.getdata())[::sample_step]
                    if not colors:continue
                    dark=sum(255-max(pixel) for pixel in colors)/len(colors)
                    if dark<best[0]:best=(dark,y)
                target=max(top+1,best[1])
            part=source.crop((0,top,w,target))
            buf=io.BytesIO();part.save(buf,format='PNG',compress_level=3)
            pieces.append(buf.getvalue())
            top=target
        return pieces


def pdf_from_mhtml(blob: bytes, reader_urls: list[str], target: Path, stop=None) -> int:
    """Readable borderless PDF. Keeps image order and original full pixels.

    Page width fixed at 720pt, page height <= ~2500pt: viewers won't shrink
    an entire chapter of 100 images into an unreadable thin strip.
    """
    import base64
    from urllib.parse import unquote_to_bytes
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader
    if stop is not None and stop.is_set():
        raise InterruptedError('Đã dừng PDF')
    check=verify_mhtml(blob, reader_urls)
    if not check.ok:
        raise ValueError('MHTML chưa đủ ảnh: '+check.explanation)
    mapping=_mhtml_images(blob)
    urls=[normalize_url(x) for x in reader_urls if x]
    if not urls:raise ValueError('Không có ảnh truyện')
    pieces=[]; sizes=[]
    for url in urls:
        if stop is not None and stop.is_set():raise InterruptedError('Đã dừng PDF')
        raw=mapping.get(url)
        if raw is None and url.startswith('data:image/'):
            prefix,comma,value=url.partition(',')
            if comma:
                raw=(base64.b64decode(value,validate=True) if ';base64' in prefix
                     else unquote_to_bytes(value))
        if not raw:raise ValueError('Thiếu tài nguyên ảnh: '+url[:120])
        for piece in _source_slices(raw):
            sizes.append(_pdf_probe(piece));pieces.append(piece)
    plan=plan_continuous_pdf(sizes)
    target.parent.mkdir(parents=True,exist_ok=True)
    tmp=target.with_name(target.name+'.part')
    tmp.unlink(missing_ok=True)
    try:
        doc=canvas.Canvas(str(tmp),pagesize=(720,1200),pageCompression=1)
        doc.setTitle(target.stem)
        for width,height,indices in plan:
            if stop is not None and stop.is_set():raise InterruptedError('Đã dừng PDF')
            doc.setPageSize((width,height))
            cursor=height
            for idx in indices:
                if stop is not None and stop.is_set():raise InterruptedError('Đã dừng PDF')
                image,_,_=_pdf_image_reader(pieces[idx])
                w,h=sizes[idx]
                dh=width*h/w
                cursor-=dh
                doc.drawImage(image,0,max(0.,cursor-0.004),width=width,height=dh+0.008,mask='auto')
            doc.showPage()
        doc.save()
        with tmp.open('rb') as inp:
            pdf=PdfReader(inp,strict=False)
            if len(pdf.pages)!=len(plan):raise RuntimeError('Số trang PDF không đúng')
            for pg in pdf.pages:
                if float(pg.mediabox.width)<300 or float(pg.mediabox.height)>2700:
                    raise RuntimeError('PDF có kích thước trang bất thường')
        if tmp.stat().st_size<1500:raise RuntimeError('PDF trống')
        if stop is not None and stop.is_set():raise InterruptedError('Đã dừng PDF')
        os.replace(tmp,target)
        return len(plan)
    finally:
        tmp.unlink(missing_ok=True)

def read_pdf_manifest(folder: Path) -> dict:
    try:
        data = json.loads((folder / 'TIEN_DO_PDF.json').read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _sha256_file(path: Path) -> str:
    import hashlib
    sha = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b''):
            sha.update(block)
    return sha.hexdigest()


def pdf_completed(folder: Path, chapter: str, url: str, target: Path) -> bool:
    entry = read_pdf_manifest(folder).get(chapter)
    if not isinstance(entry, dict) or not target.is_file():
        return False
    if not (entry.get('url') == url and entry.get('file') == target.name
            and entry.get('source_count', 0) > 0 and entry.get('pages', 0) > 0
            and entry.get('render_version') == PDF_RENDER_VERSION
            and target.stat().st_size > 1500 and target.stat().st_size == entry.get('bytes')):
        return False
    return _sha256_file(target) == entry.get('sha256')


def write_pdf_manifest(folder: Path, chapter: str, url: str, path: Path,
                       image_count: int, page_count: int) -> None:
    with MANIFEST_LOCK:
        folder.mkdir(parents=True, exist_ok=True)
        data = read_pdf_manifest(folder)
        data[chapter] = {'url': url, 'file': path.name, 'bytes': path.stat().st_size,
                         'source_count': image_count, 'pages': page_count,
                         'sha256': _sha256_file(path),
                         'render_version': PDF_RENDER_VERSION, 'version': VERSION, 'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')}
        tmp = folder / 'TIEN_DO_PDF.json.tmp'
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, folder / 'TIEN_DO_PDF.json')


def combined_pdf_name(chapters: list[dict]) -> str:
    first = chapter_label(chapters[0]['number'])
    last = chapter_label(chapters[-1]['number'])
    return f'Truyen_gop_Chap_{first}-{last}.pdf'


def merge_chapter_pdfs(folder: Path, chapters: list[dict], output: Path, stop=None) -> int:
    """Merge only when every selected chapter PDF is available and accounted for."""
    from pypdf import PdfReader, PdfWriter
    missing = []
    for chapter in chapters:
        number = chapter_label(chapter['number'])
        if not pdf_completed(folder, number, chapter['url'], folder / f'Chap_{number}.pdf'):
            missing.append(number)
    if missing:
        raise RuntimeError(f'Không gộp để tránh PDF thiếu chap. Chưa có: {", ".join(missing[:20])}')
    writer = PdfWriter()
    try:
        total = 0
        for item in chapters:
            if stop is not None and stop.is_set():
                raise InterruptedError('Đã dừng gộp PDF')
            path = folder / f'Chap_{chapter_label(item["number"])}.pdf'
            reader = PdfReader(str(path), strict=False)
            count = len(reader.pages)
            if count < 1:
                raise ValueError(f'{path.name} không có trang')
            writer.append(reader)
            total += count
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_name(output.name + '.part')
        try:
            with temp.open('wb') as stream:
                writer.write(stream)
            with temp.open('rb') as stream:
                if len(PdfReader(stream, strict=False).pages) != total:
                    raise RuntimeError('Số trang PDF gộp không khớp')
            if stop is not None and stop.is_set():
                raise InterruptedError('Đã dừng trước khi hoàn tất PDF gộp')
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
        return total
    finally:
        writer.close()


def merged_cache_folder(folder: Path, chapters: list[dict]) -> Path:
    import hashlib
    data='\n'.join(str(c['number'])+'|'+c['url'] for c in chapters)
    digest=hashlib.sha256(data.encode('utf-8')).hexdigest()[:12]
    return folder / '.truyensaver_cache' / digest


def merged_completed(folder: Path, chapters: list[dict], output: Path) -> bool:
    metadata=folder / 'TIEN_DO_GOP.json'
    try:
        entry=json.loads(metadata.read_text(encoding='utf-8'))
        expected=[str(c['number'])+'|'+c['url'] for c in chapters]
        return (entry.get('input') == expected and
                entry.get('render_version') == PDF_RENDER_VERSION and
                entry.get('output') == output.name and output.is_file() and
                output.stat().st_size == entry.get('size') and
                entry.get('sha256') == _sha256_file(output))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def record_merged(folder: Path, chapters: list[dict], output: Path) -> None:
    info={'input':[str(c['number'])+'|'+c['url'] for c in chapters],
          'render_version':PDF_RENDER_VERSION,'output':output.name,
          'size':output.stat().st_size,'sha256':_sha256_file(output),
          'timestamp':time.strftime('%Y-%m-%d %H:%M:%S')}
    tmp=folder/'TIEN_DO_GOP.json.tmp'
    tmp.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(tmp,folder/'TIEN_DO_GOP.json')

def write_index_formats(folder: Path, title: str, chapters: list[dict], fmt: str,
                        combined: Path | None = None, require_ad_free: bool = False) -> None:
    use_mhtml = fmt == 'mhtml'
    use_pdf = fmt == 'pdf'
    entries = []
    for chap in chapters:
        number = chapter_label(chap['number'])
        links = []
        if use_mhtml and completed(folder, number, chap['url'], folder / f'Chap_{number}.mhtml', require_ad_free=require_ad_free):
            links.append(f'<a href="Chap_{html.escape(number, quote=True)}.mhtml">MHTML (offline)</a>')
        if use_pdf and pdf_completed(folder, number, chap['url'], folder / f'Chap_{number}.pdf'):
            links.append(f'<a href="Chap_{html.escape(number, quote=True)}.pdf">PDF</a>')
        if links:
            entries.append(f'<li>Chap {html.escape(number)}: {" | ".join(links)}</li>')
    combined_link = (f'<p><strong><a href="{html.escape(combined.name, quote=True)}">'
                     'PDF gộp toàn bộ chương</a></strong></p>'
                     if combined is not None and combined.is_file() else '')
    source = '<!doctype html><html lang="vi"><meta charset="utf-8"><title>Mục lục truyện</title>'
    source += ('<style>body{font:18px Segoe UI,Arial;max-width:760px;margin:40px auto;'
               'padding:16px;line-height:1.7}a{color:#1659a7}li{margin:10px 0}</style>')
    source += f'<h1>{html.escape(title)}</h1>{combined_link}<ol>{"".join(entries)}</ol>'
    source += '<p>Hãy mở thử các chương khi không có mạng để kiểm tra tài nguyên.</p></html>'
    (folder / 'MUC_LUC.html').write_text(source, encoding='utf-8')


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f'TruyenSaver PRO v{VERSION} — Hsin Moon Fox')
        self.root.geometry('1050x890')
        self.root.minsize(950, 740)
        self.eventq = queue.Queue()
        self.cancel = threading.Event()
        self._futures_lock = threading.RLock()
        self._futures = set()
        self.busy = False
        self.settings = self._load_settings()
        self.browser = ChromiumEngine()
        self.folder_var = tk.StringVar(value=self.settings.get('folder', str(Path.home() / 'Downloads' / 'Truyen_Offline')))
        self.start_var = tk.StringVar(value=str(self.settings.get('start', '1')))
        self.end_var = tk.StringVar(value=str(self.settings.get('end', '100')))
        self.status_var = tk.StringVar(value='Sẵn sàng – hãy thử 1–2 chap trước')
        self.format_var = tk.StringVar(value=self.settings.get('format', 'MHTML (đọc offline)'))
        if self.format_var.get() not in ('MHTML (đọc offline)', 'PDF riêng (mỗi chap)', 'PDF gộp (1 tệp)'):
            self.format_var.set('MHTML (đọc offline)')
        self.workers_var = tk.StringVar(value=str(self.settings.get('workers', '2')))
        self.ad_free_var = tk.BooleanVar(value=bool(self.settings.get('remove_ads', True)))
        self.theme_var = tk.StringVar(value=self.settings.get('theme', 'Hsin • Moon Fox'))
        self.scanned = {}
        self.scan_selection = set()
        self.selection_snapshot = None
        self.row_key_to_ref = {}
        self.chapter_states = {}
        self.host_health = HostHealth()
        self._ui()
        self.links.insert('1.0', self.settings.get('links', 'https://damconuong.cool/truyen/ban-hoc'))
        root.after(120, self._drain)
        root.protocol('WM_DELETE_WINDOW', self._close)

    def _ui(self):
        self.main=ttk.Frame(self.root,padding=(18,12))
        self.main.pack(fill='both',expand=True)
        self.banner=tk.Canvas(self.main,height=122,bd=0,highlightthickness=0)
        self.banner.pack(fill='x',pady=(0,10))
        self._paint_banner()
        headline=ttk.Frame(self.main)
        headline.pack(fill='x',pady=(0,6))
        ttk.Label(headline,text='Nguồn truyện · mỗi dòng một đường dẫn',style='Header.TLabel').pack(side='left')
        ttk.Label(headline,text='Giao diện',style='Muted.TLabel').pack(side='right',padx=(8,2))
        self.theme_box=ttk.Combobox(headline,textvariable=self.theme_var,state='readonly',
                    values=('Hsin • Moon Fox','Tối','Sáng'),width=17)
        self.theme_box.pack(side='right')
        self.theme_box.bind('<<ComboboxSelected>>',self._change_theme)
        self.links=tk.Text(self.main,height=2,font=('Segoe UI',10),wrap='word',relief='flat',padx=10,pady=7)
        self.links.pack(fill='x',pady=(2,8))
        options=ttk.Frame(self.main)
        options.pack(fill='x',pady=(2,5))
        ttk.Label(options,text='Từ chap').pack(side='left')
        ttk.Entry(options,textvariable=self.start_var,width=8).pack(side='left',padx=(5,10))
        ttk.Label(options,text='Đến').pack(side='left')
        ttk.Entry(options,textvariable=self.end_var,width=8).pack(side='left',padx=(5,16))
        ttk.Label(options,text='Chế độ').pack(side='left')
        self.mode_box=ttk.Combobox(options,textvariable=self.format_var,state='readonly',
                        values=('MHTML (đọc offline)','PDF riêng (mỗi chap)','PDF gộp (1 tệp)'),width=24)
        self.mode_box.pack(side='left',padx=(5,10))
        ttk.Label(options,text='Song song').pack(side='left')
        self.workers_box=ttk.Combobox(options,textvariable=self.workers_var,state='readonly',
                                     values=('1','2','3'),width=3)
        self.workers_box.pack(side='left',padx=(5,8))
        folder_line=ttk.Frame(self.main)
        folder_line.pack(fill='x',pady=(3,6))
        ttk.Label(folder_line,text='Thư mục lưu').pack(side='left',padx=(0,8))
        ttk.Entry(folder_line,textvariable=self.folder_var).pack(side='left',fill='x',expand=True)
        ttk.Button(folder_line,text='Chọn thư mục',command=self._choose_folder).pack(side='left',padx=(6,4))
        ttk.Button(folder_line,text='Mở',command=self._open_folder).pack(side='left')
        ttk.Checkbutton(self.main,text='Lọc quảng cáo trước khi lưu (không sửa ảnh gốc)',
                        variable=self.ad_free_var).pack(anchor='w',pady=(0,8))
        controls=ttk.Frame(self.main)
        controls.pack(fill='x',pady=(0,9))
        self.buttons=[]
        for label,func in [('Đăng nhập (nếu cần)',self.login),('QUÉT DANH SÁCH CHAP',self.scan),
                           ('BẮT ĐẦU TẢI',self.download)]:
            btn=ttk.Button(controls,text=label,command=func,
                           style='Primary.TButton' if func==self.download else 'TButton')
            btn.pack(side='left',padx=(0,8))
            self.buttons.append(btn)
        self.stop_button=ttk.Button(controls,text='DỪNG TẤT CẢ',command=self.stop,state='disabled',style='Stop.TButton')
        self.stop_button.pack(side='left')
        ttk.Button(controls,text='ĐỌC OFFLINE',command=self._read_offline).pack(side='right')
        table_head=ttk.Frame(self.main)
        table_head.pack(fill='x')
        ttk.Label(table_head,text='Danh sách chương · chọn các chap cần tải',style='Header.TLabel').pack(side='left')
        self.count_var=tk.StringVar(value='Chưa quét')
        ttk.Label(table_head,textvariable=self.count_var,style='Muted.TLabel').pack(side='right')
        actions=ttk.Frame(self.main)
        actions.pack(fill='x',pady=(5,5))
        ttk.Button(actions,text='Chọn tất cả',command=self._select_all).pack(side='left',padx=(0,5))
        ttk.Button(actions,text='Bỏ chọn',command=self._select_none).pack(side='left',padx=(0,5))
        ttk.Button(actions,text='Chọn khoảng',command=self._select_range).pack(side='left',padx=(0,5))
        self.filter_var=tk.StringVar()
        ttk.Label(actions,text='Lọc:').pack(side='left',padx=(14,4))
        search=ttk.Entry(actions,textvariable=self.filter_var,width=25)
        search.pack(side='left')
        self.filter_var.trace_add('write',lambda *_:self._draw_chapters())
        table_box=ttk.Frame(self.main)
        table_box.pack(fill='both',expand=True)
        self.table=ttk.Treeview(table_box,columns=('chosen','source','series','chap','status'),
                               show='headings',selectmode='browse',height=9)
        for col,label,width,anchor in [('chosen','Tải',50,'center'),('source','Website',150,'w'),
                    ('series','Tên truyện',215,'w'),('chap','Chương',90,'center'),
                    ('status','Trạng thái',180,'w')]:
            self.table.heading(col,text=label)
            self.table.column(col,width=width,minwidth=45,anchor=anchor,
                              stretch=col in ('series','status'))
        self.table.pack(side='left',fill='both',expand=True)
        bar=ttk.Scrollbar(table_box,orient='vertical',command=self.table.yview)
        bar.pack(side='right',fill='y')
        self.table.configure(yscrollcommand=bar.set)
        self.table.bind('<Button-1>',self._click_chapter)
        self.status_var.set('Sẵn sàng — quét danh sách, chọn chap rồi tải')
        ttk.Label(self.main,textvariable=self.status_var,style='Muted.TLabel').pack(anchor='w',pady=(7,2))
        self.progress=ttk.Progressbar(self.main,mode='determinate')
        self.progress.pack(fill='x',pady=(0,7))
        ttk.Label(self.main,text='Nhật ký chi tiết',style='Header.TLabel').pack(anchor='w')
        self.logs=scrolledtext.ScrolledText(self.main,height=8,font=('Consolas',9),
                  state='disabled',wrap='word',relief='flat',padx=9,pady=7)
        self.logs.pack(fill='both',expand=False,pady=(4,4))
        ttk.Label(self.main,text='PDF tối ưu chiều rộng · kiểm tra MHTML · lưu tiến độ · phục hồi khi mất mạng. '
                  'Nếu website chặn truy cập, ứng dụng sẽ báo lỗi thay vì vượt qua hạn chế.',
                  style='Muted.TLabel',wraplength=970).pack(anchor='w',pady=(2,0))
        self._change_theme(save=False)

    def _paint_banner(self):
        palette=THEMES.get(self.theme_var.get(), THEMES['Hsin • Moon Fox'])
        self.banner.configure(bg=palette['banner'])
        width=max(960,self.root.winfo_width()-40)
        self.banner.delete('all')
        try:
            from PIL import Image, ImageTk
            if self.theme_var.get().startswith('Hsin'):
                import base64,io
                src=asset_path('moon_fox_banner.png')
                art=(src.read_bytes() if src.is_file() else
                     base64.b64decode(MOON_FOX_BANNER_PNG_BASE64))
                photo=Image.open(io.BytesIO(art)).convert('RGB')
                photo=photo.resize((width,122),Image.Resampling.LANCZOS)
                self._banner_photo=ImageTk.PhotoImage(photo)
                self.banner.create_image(0,0,anchor='nw',image=self._banner_photo)
        except Exception:
            pass
        color=palette['gold'] if self.theme_var.get().startswith('Hsin') else palette['text']
        self.banner.create_text(25,35,text='TruyenSaver PRO',anchor='w',
                                fill=color,font=('Segoe UI',23,'bold'))
        self.banner.create_text(27,73,text=f'v{VERSION}   •   MOON FOX EDITION   •   CHROMIUM OFFLINE',
                                anchor='w',fill=palette['muted'],font=('Segoe UI',10,'bold'))
        self.banner.create_text(27,99,text='TẢI ỔN ĐỊNH   /   PDF DỄ ĐỌC   /   CHỌN CHAP TỰ DO',
                                anchor='w',fill=color,font=('Segoe UI',9))

    def _change_theme(self,event=None,save=True):
        palette=THEMES.get(self.theme_var.get(),THEMES['Hsin • Moon Fox'])
        apply_app_style(self.root,self.theme_var.get())
        for widget in (self.links,self.logs):
            widget.configure(background=palette['input'],foreground=palette['text'],
                             insertbackground=palette['text'])
        self._paint_banner()
        if save:
            try:self._save_settings()
            except Exception:pass

    def _draw_chapters(self):
        term=self.filter_var.get().strip().casefold() if hasattr(self,'filter_var') else ''
        for item in self.table.get_children():self.table.delete(item)
        self.row_key_to_ref={}
        for url,rec in self.scanned.items():
            for chap in rec['chapters']:
                ref=(url,chap['url'])
                name='Chap '+chapter_label(chap['number'])
                if term and term not in (' '.join((rec['title'],name,url))).casefold():continue
                key=f'r{len(self.row_key_to_ref)}'
                self.row_key_to_ref[key]=ref
                self.table.insert('', 'end',iid=key,values=(
                    '☑' if ref in self.scan_selection else '☐',
                    urlsplit(url).hostname,rec['title'],name,
                    self.chapter_states.get(ref,'Chưa tải')))
        self._count_chapters()

    def _count_chapters(self):
        total=sum(len(v['chapters']) for v in self.scanned.values())
        self.count_var.set(f'Đã chọn {len(self.scan_selection)} / {total} chap')

    def _click_chapter(self,event):
        row=self.table.identify_row(event.y)
        col=self.table.identify_column(event.x)
        if not row or col!='#1' or self.busy:return
        ref=self.row_key_to_ref.get(row)
        if not ref:return
        if ref in self.scan_selection:self.scan_selection.discard(ref)
        else:self.scan_selection.add(ref)
        self.table.set(row,'chosen','☑' if ref in self.scan_selection else '☐')
        self._count_chapters()

    def _select_all(self):
        if self.busy:return
        self.scan_selection={(u,c['url']) for u,v in self.scanned.items() for c in v['chapters']}
        self._draw_chapters()

    def _select_none(self):
        if self.busy:return
        self.scan_selection.clear();self._draw_chapters()

    def _select_range(self):
        if self.busy:return
        try:
            lo=float(self.start_var.get().replace(',','.'))
            hi=float(self.end_var.get().replace(',','.'))
            if lo>hi:raise ValueError()
        except ValueError:
            messagebox.showerror('Khoảng chap','Nhập số chap bắt đầu và kết thúc hợp lệ.')
            return
        self.scan_selection={(u,c['url']) for u,v in self.scanned.items()
                             for c in v['chapters'] if lo<=c['number']<=hi}
        self._draw_chapters()

    def _load_settings(self):
        for path in (SETTINGS, PREVIOUS_SETTINGS, LEGACY_SETTINGS):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    return data
            except (OSError, TypeError, ValueError):
                continue
        return {}

    def _save_settings(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps({'folder': self.folder_var.get().strip(),
            'start': self.start_var.get().strip(),
            'end': self.end_var.get().strip(),
            'links': self.links.get('1.0','end').strip(),
            'format': self.format_var.get(), 'workers': self.workers_var.get(),
            'remove_ads': bool(self.ad_free_var.get()),
            'theme': self.theme_var.get()}, indent=2, ensure_ascii=False), encoding='utf-8')

    def _choose_folder(self):
        path = filedialog.askdirectory(title='Chọn thư mục lưu truyện',
            initialdir=self.folder_var.get() if Path(self.folder_var.get()).is_dir() else str(Path.home()))
        if path:
            self.folder_var.set(path)
            self._save_settings()

    def _open_folder(self):
        path = Path(self.folder_var.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(str(path))
        else:
            messagebox.showinfo('Thư mục', str(path))

    def _read_offline(self):
        """Open newest saved comic index in bundled Chromium, no browser install."""
        folder=Path(self.folder_var.get().strip()).expanduser()
        if not folder.is_dir():
            messagebox.showinfo('Chưa có truyện','Hãy chọn thư mục truyện đã lưu.')
            return
        indexes=sorted(folder.rglob('MUC_LUC.html'),
                       key=lambda path:path.stat().st_mtime,reverse=True)
        if not indexes:
            messagebox.showinfo('Chưa có mục lục MHTML',
                                'Chưa tìm thấy MUC_LUC.html trong thư mục. '                                'Nếu bạn chọn PDF, hãy dùng Mở thư mục để mở tệp PDF.')
            return
        try:
            executable=chromium_executable()
            reader_profile=CONFIG_DIR/'OfflineReaderProfile'
            reader_profile.mkdir(parents=True,exist_ok=True)
            args=[executable,'--no-first-run','--no-default-browser-check',
                  '--user-data-dir='+str(reader_profile),
                  '--allow-file-access-from-files', indexes[0].as_uri()]
            if sys.platform != 'win32' and hasattr(os,'geteuid') and os.geteuid()==0:
                args.insert(1,'--no-sandbox')
            subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                             creationflags=(0x08000000 if sys.platform=='win32' else 0))
        except Exception as exc:
            messagebox.showerror('Không mở được trình đọc',str(exc))

    def _emit(self, kind: str, *args):
        self.eventq.put((kind, args))

    def log(self, message: str):
        self._emit('log', message)

    def _drain(self):
        try:
            while True:
                kind, args = self.eventq.get_nowait()
                if kind == 'log':
                    self.logs.configure(state='normal')
                    self.logs.insert('end', f'[{time.strftime("%H:%M:%S")}] {args[0]}\n')
                    self.logs.see('end')
                    self.logs.configure(state='disabled')
                elif kind == 'scanned':
                    source,title,chapters=args
                    self.scanned[source]={'title':title,'chapters':chapters}
                    self.scan_selection.update((source,c['url']) for c in chapters)
                    self._draw_chapters()
                elif kind == 'chapter_state':
                    source,chapter_url,state=args
                    self.chapter_states[(source,chapter_url)]=state
                    for row,ref in self.row_key_to_ref.items():
                        if ref==(source,chapter_url):
                            self.table.set(row,'status',state)
                            break
                elif kind == 'progress':
                    current, total, message = args
                    self.progress.configure(maximum=max(1,total), value=current)
                    self.status_var.set(message)
                elif kind == 'finish':
                    self.busy = False
                    for b in self.buttons:
                        b.configure(state='normal')
                    self.stop_button.configure(state='disabled')
                    self.status_var.set(args[0] or 'Sẵn sàng')
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _fields(self):
        urls = list(dict.fromkeys(x.strip() for x in self.links.get('1.0','end').splitlines() if x.strip()))
        if not urls:
            raise ValueError('Hãy dán ít nhất một link truyện.')
        for url in urls:
            validate_source(url)
        try:
            start = float(self.start_var.get().replace(',','.'))
            end = float(self.end_var.get().replace(',','.'))
        except ValueError:
            raise ValueError('Số chap không hợp lệ.')
        if not (0 <= start <= end):
            raise ValueError('Khoảng chap phải tăng dần và không âm.')
        folder = Path(self.folder_var.get().strip()).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        self._save_settings()
        modes = {'MHTML (đọc offline)': 'mhtml', 'PDF riêng (mỗi chap)': 'pdf', 'PDF gộp (1 tệp)': 'merged'}
        mode = modes[self.format_var.get()]
        workers = int(self.workers_var.get())
        if workers not in (1, 2, 3):
            raise ValueError('Số luồng phải từ 1 đến 3')
        return urls, start, end, folder, mode, workers, bool(self.ad_free_var.get())

    def _run(self, action):
        if self.busy:
            return
        try:
            urls, start, end, folder, mode, workers, strip_ads = self._fields()
        except Exception as exc:
            messagebox.showerror('Thiếu thông tin', str(exc))
            return
        self.busy = True
        self.cancel.clear()
        for b in self.buttons:
            b.configure(state='disabled')
        self.stop_button.configure(state='normal')
        def worker():
            msg='Xong'
            try:
                msg = action(urls, start, end, folder, mode, workers, strip_ads) or msg
            except InterruptedError:
                self.log('Đã dừng theo yêu cầu. Các chap đã lưu vẫn được giữ lại.')
                msg='Đã dừng'
            except Exception as exc:
                if self.cancel.is_set():
                    self.log('Đã hủy tác vụ và ngắt kết nối Chromium.')
                    msg='Đã dừng'
                else:
                    self.log('LỖI: '+str(exc))
                    msg='Có lỗi – xem nhật ký'
            finally:
                self._emit('finish', msg)
        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        if not self.busy or self.cancel.is_set():
            return
        self.cancel.set()
        self.stop_button.configure(state='disabled')
        self.status_var.set('Đang dừng mọi tác vụ…')
        self.log('DỪNG: hủy toàn bộ chap chờ, ngắt Chromium và ngừng tải/PDF.')
        with self._futures_lock:
            for future in tuple(self._futures):
                future.cancel()
        # Killing the browser interrupts in-flight page.goto / CDP calls.
        threading.Thread(target=self.browser.abort, name='stop-chromium',daemon=True).start()

    def login(self):
        def action(urls,start,end,folder,mode,workers,strip_ads):
            self.browser.ensure(visible=True)
            self.log('Đã mở Chromium với hồ sơ riêng. Hãy đăng nhập nếu website yêu cầu.')
            self.log('Đóng cửa sổ Chromium đăng nhập trước khi bấm Quét/Bắt đầu; hồ sơ vẫn được lưu.')
            return 'Đã mở Chromium – hãy đăng nhập rồi tiếp tục'
        self._run(action)

    def scan(self):
        self.scanned.clear()
        self.scan_selection.clear()
        self.chapter_states.clear()
        self._draw_chapters()
        self._run(self._scan)

    def download(self):
        self.selection_snapshot = (set(self.scan_selection) if self.scanned else None)
        self._run(self._download)

    def _connect(self):
        from playwright.sync_api import sync_playwright
        port = self.browser.ensure(visible=False)
        return port, sync_playwright()

    def _navigate_to_chapter(self, page, url):
        """Fast navigation resilient to resources hanging forever on weak networks."""
        from playwright.sync_api import TimeoutError as PWTimeout
        self.host_health.wait(url,self.cancel)
        if self.cancel.is_set():raise InterruptedError('Đã dừng')
        try:
            response=page.goto(url,wait_until='commit',timeout=21000)
        except PWTimeout as error:
            if self.cancel.is_set():raise InterruptedError('Đã dừng') from error
            # Navigating can time out even if the page is already rendering.
            current=page.url
            live=False
            try:
                live=(urlsplit(current).path.rstrip('/')==urlsplit(url).path.rstrip('/')
                      and bool(page.evaluate('Boolean(document.body && (document.images.length || document.querySelector("main,article")))')))
            except Exception:pass
            if not live:raise
            self.log('Trang phản hồi chậm; đã thấy nội dung, tiếp tục kiểm tra ảnh.')
            response=None
        if response and response.status in (401,403):
            raise PermissionError(f'Website yêu cầu đăng nhập hoặc từ chối HTTP {response.status}')
        if response and response.status in (429,500,502,503,504):
            raise RuntimeError(f'Website quá tải / giới hạn tốc độ HTTP {response.status}')
        return response

    def _discover(self, page, url, start, end):
        self.log('Đang quét: '+url)
        self._navigate_to_chapter(page,url)
        # Don't assume a slow site finishes rendering chapter anchors in 700ms.
        # Wait for actual chapter anchors; allow a small scroll to trigger lazy lists.
        chapters=[]
        steady=0
        previous_size=-1
        for attempt in range(26):
            if self.cancel.is_set():raise InterruptedError('Đã dừng quét')
            try:
                anchors = page.locator('a[href]').evaluate_all(
                    "els => els.map(a=>({href:a.href,text:(a.textContent||'').trim()}))")
                chapters = [c for c in extract_chapters(anchors,url)
                            if start <= c['number'] <= end]
            except Exception as ex:
                if attempt>=25:raise RuntimeError('Không đọc được danh sách chap: '+str(ex))
            if chapters and len(chapters)==previous_size:
                steady+=1
            else:
                steady=0
            previous_size=len(chapters)
            if chapters and steady>=3:break
            if attempt==6:
                try:page.evaluate('window.scrollBy(0, Math.max(500, innerHeight))')
                except Exception:pass
            page.wait_for_timeout(350)
        if not chapters:
            raise RuntimeError('Không tìm được chap. Website có thể tải chậm, yêu cầu đăng nhập hoặc thay đổi cấu trúc.')
        try:
            title = page.locator('h1').first.text_content(timeout=2500) or ''
        except Exception:
            title = ''
        title = sanitize(title.strip() or urlsplit(url).path.rstrip('/').rsplit('/',1)[-1])
        self.log(f'{title}: {len(chapters)} chap trong khoảng {start:g}–{end:g}.')
        present = {int(c['number']) for c in chapters if c['number'].is_integer()}
        if len(present) == len(chapters) and present:
            missing_nums = [n for n in range(min(present), max(present)+1) if n not in present]
            if missing_nums:
                self.log('    Website không liệt kê chap: ' + ', '.join(map(str,missing_nums[:25]))
                         + ('...' if len(missing_nums)>25 else ''))
        return title, chapters

    def _scan(self, urls, start, end, folder, mode, workers, strip_ads):
        port, handle = self._connect()
        with handle as play:
            browser = play.chromium.connect_over_cdp(f'http://127.0.0.1:{port}', timeout=15000)
            page = browser.contexts[0].new_page()
            try:
                for url in urls:
                    if self.cancel.is_set():
                        raise InterruptedError()
                    try:
                        title, chapters = self._discover(page, url, 0, float('inf'))
                        self._emit('scanned', url, title, chapters)
                    except Exception as exc:
                        self.log(f'Không quét được {url}: {exc}')
                        if self.cancel.is_set():raise InterruptedError()
            finally:
                page.close()
        return 'Đã quét xong'

    def _process_chapter(self, port, dest: Path, chap: dict, mode: str,
                         strip_ads: bool, source_dir: Path | None = None) -> tuple[bool, str]:
        """Independent CDP connection per worker; no global mouse/keyboard use."""
        from playwright.sync_api import sync_playwright
        if self.cancel.is_set():
            raise InterruptedError('Đã dừng trước khi bắt đầu chap')
        number=chapter_label(chap['number'])
        url=chap['url']
        source_dir=source_dir or dest
        archive=source_dir/f'Chap_{number}.mhtml'
        target=dest/f'Chap_{number}.pdf'
        if mode=='mhtml' and completed(source_dir,number,url,archive,require_ad_free=strip_ads):
            return True, f'Chap {number}: MHTML đã có và hợp lệ.'
        if mode in ('pdf','merged') and pdf_completed(dest,number,url,target):
            return True, f'Chap {number}: PDF đã có và hợp lệ.'
        # Reuse older verified MHTML if PDF is required; never WRITE an
        # unwanted MHTML when user selects PDF-only.
        if mode!='mhtml' and completed(source_dir,number,url,archive):
            try:
                blob=archive.read_bytes()
                entry=read_manifest(source_dir).get(number) or {}
                img_urls=entry.get('reader_urls') or offline_reader_urls(blob,url,int(entry.get('expected') or 0))
                if img_urls and verify_mhtml(blob,img_urls).ok:
                    pages=pdf_from_mhtml(blob,img_urls,target,self.cancel)
                    if self.cancel.is_set():
                        raise InterruptedError('Đã dừng trước khi xác nhận PDF')
                    write_pdf_manifest(dest,number,url,target,len(img_urls),pages)
                    return True, f'Chap {number}: tái sử dụng MHTML cũ, PDF {pages} trang.'
            except InterruptedError:
                raise
            except Exception as exc:
                self.log(f'Chap {number}: MHTML cũ chưa đủ, tải lại ({exc}).')
        with sync_playwright() as driver:
            browser=driver.chromium.connect_over_cdp(f'http://127.0.0.1:{port}',timeout=20000)
            if self.cancel.is_set():
                raise InterruptedError('Đã dừng trước khi mở trang')
            page=browser.contexts[0].new_page()
            try:
                for attempt in range(3):
                    if self.cancel.is_set():
                        raise InterruptedError('Đã yêu cầu dừng')
                    try:
                        self.log(f'Chap {number}: tải lượt {attempt+1}/3 ...')
                        response=self._navigate_to_chapter(page,url)
                        if response and response.status in (401,403):
                            raise RuntimeError(f'Website yêu cầu đăng nhập hoặc từ chối HTTP {response.status}')
                        if response and response.status in (429,500,502,503,504):
                            raise RuntimeError(f'Website quá tải / giới hạn tốc độ HTTP {response.status}')
                        page.wait_for_timeout(350)
                        reader=wait_for_reader(page,self.log,self.cancel,max_seconds=(65,90,110)[attempt])
                        blob,check=capture_verified_blob(page,reader,self.log,strip_ads=strip_ads)
                        if self.cancel.is_set():
                            raise InterruptedError('Đã dừng trước khi lưu chap')
                        if mode=='mhtml':
                            save_atomic_bytes(blob,archive,self.cancel)
                            if self.cancel.is_set():
                                raise InterruptedError('Đã dừng trước khi xác nhận chap')
                            write_manifest_entry(source_dir,number,url,archive,check.expected,check.matched,
                                                 reader['urls'],ads_filtered=strip_ads)
                            if not completed(source_dir,number,url,archive,require_ad_free=strip_ads):
                                raise RuntimeError('Chưa xác nhận được MHTML đã lưu')
                            self.host_health.succeeded(url)
                            return True,f'Chap {number}: lưu MHTML đủ {check.matched} ảnh.'
                        pages=pdf_from_mhtml(blob,reader['urls'],target,self.cancel)
                        if self.cancel.is_set():
                            raise InterruptedError('Đã dừng trước khi xác nhận PDF')
                        write_pdf_manifest(dest,number,url,target,len(reader['urls']),pages)
                        if not pdf_completed(dest,number,url,target):
                            raise RuntimeError('Chưa xác nhận được PDF đã lưu')
                        self.host_health.succeeded(url)
                        return True,f'Chap {number}: PDF {pages} trang, {check.matched} ảnh.'
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        if self.cancel.is_set():
                            raise InterruptedError('Đã dừng toàn bộ tác vụ') from exc
                        self.log(f'Chap {number}: lỗi lượt {attempt+1}: {exc}')
                        if isinstance(exc, PermissionError):
                            raise  # Do not retry access-denied pages.
                        delay=self.host_health.failed(url)
                        if attempt==2:
                            raise
                        self.log(f'    Mạng/trang phản hồi chậm: chờ {delay:.1f}s trước khi thử lại.')
                        self.host_health.wait(url,self.cancel)
                        try: page.goto('about:blank',wait_until='commit',timeout=5000)
                        except Exception:pass
            finally:
                try: page.close()
                except Exception: pass

    def _download(self, urls, start, end, root, mode, workers, strip_ads):
        """One fair global queue for all sites. Two different sites can run at once."""
        import shutil
        from itertools import zip_longest
        from concurrent.futures import ThreadPoolExecutor, as_completed

        port, handle=self._connect()
        specifications=[]; results=[]
        # Discover sequentially (lightweight), then interleave chapter jobs
        # across series to avoid starving the second website.
        with handle as driver:
            browser=driver.chromium.connect_over_cdp(f'http://127.0.0.1:{port}',timeout=20000)
            scan_page=browser.contexts[0].new_page()
            try:
                for series_url in urls:
                    if self.cancel.is_set():
                        raise InterruptedError('Đã yêu cầu dừng')
                    try:
                        if series_url in self.scanned:
                            saved=self.scanned[series_url]
                            title=saved['title']
                            chapters=list(saved['chapters'])
                        else:
                            title,chapters=self._discover(scan_page,series_url,start,end)
                    except Exception as exc:
                        self.log(f'Không quét được {series_url}: {exc}')
                        results.append(series_url+': không tìm được chap')
                        continue
                    # Honor per-chapter selection after a full scan.
                    if self.scanned and series_url in self.scanned and self.selection_snapshot is not None:
                        selected=self.selection_snapshot
                        chapters=[c for c in chapters if (series_url,c['url']) in selected]
                        if not chapters:
                            self.log(f'{title}: không chọn chap nào; bỏ qua bộ truyện này.')
                            continue
                    dest=root/sanitize(urlsplit(series_url).hostname or 'web')/title
                    dest.mkdir(parents=True,exist_ok=True)
                    combined=dest/combined_pdf_name(chapters)
                    if mode=='merged' and merged_completed(dest,chapters,combined):
                        self.log(f'{title}: đã có PDF gộp hợp lệ, bỏ qua toàn bộ.')
                        results.append(f'{title}: PDF gộp đã có')
                        continue
                    work_dir=merged_cache_folder(dest,chapters) if mode=='merged' else dest
                    work_dir.mkdir(parents=True,exist_ok=True)
                    spec={'url':series_url,'title':title,'chapters':chapters,'dest':dest,
                          'cache':work_dir,'combined':combined,'done':0,'failed':[]}
                    specifications.append(spec)
                    self.log(f'{title}: {len(chapters)} chap, chế độ {mode}.')
            finally:
                scan_page.close()

            if not specifications:
                return ' | '.join(results) or 'Không tìm được chương để tải.'
            workers=max(1,min(3,int(workers)))
            total=sum(len(spec['chapters']) for spec in specifications)
            finished=0
            self.log(f'Bắt đầu {total} chap từ {len(specifications)} nguồn; {workers} tác vụ song song.')
            # Submit at most `workers` jobs, not all 100 chapters at once.
            # DỪNG cancels queued work and aborts in-flight browser operations.
            from collections import deque
            from concurrent.futures import wait, FIRST_COMPLETED, CancelledError
            items=deque((spec,chap) for row in zip_longest(*(s['chapters'] for s in specifications))
                        for spec,chap in zip(specifications,row) if chap is not None)
            pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='truyensaver')
            in_flight={}
            try:
                while items or in_flight:
                    if self.cancel.is_set():
                        raise InterruptedError('Đã dừng tất cả chap')
                    # Reduce concurrency after repeated network failures on any active host.
                    cap=1 if any(self.host_health.reduced_concurrency(spec['url'])
                                 for spec,chap in items) else workers
                    while items and len(in_flight) < cap and not self.cancel.is_set():
                        spec,chap=items.popleft()
                        future=pool.submit(self._process_chapter,port,spec['cache'],chap,
                                           mode,strip_ads,spec['dest'])
                        in_flight[future]=(spec,chap)
                        self._emit('chapter_state',spec['url'],chap['url'],'Đang tải…')
                        with self._futures_lock:
                            self._futures.add(future)
                    if not in_flight:
                        break
                    done,_=wait(tuple(in_flight),timeout=.2,return_when=FIRST_COMPLETED)
                    for future in done:
                        spec,chap=in_flight.pop(future)
                        with self._futures_lock:
                            self._futures.discard(future)
                        if self.cancel.is_set():
                            raise InterruptedError('Đã dừng mọi tác vụ')
                        number=chapter_label(chap['number'])
                        try:
                            ok,message=future.result()
                            if ok: spec['done']+=1
                            self._emit('chapter_state',spec['url'],chap['url'],
                                       'Đã hoàn thành' if ok else 'Chưa hoàn thành')
                            self.log(spec['title']+': '+message)
                        except (InterruptedError,CancelledError):
                            self.cancel.set()
                            raise InterruptedError('Đã dừng mọi tác vụ')
                        except Exception as exc:
                            item=f'Chap {number}: {exc}'
                            spec['failed'].append(item)
                            self._emit('chapter_state',spec['url'],chap['url'],'Lỗi — thử lại sau')
                            self.log(spec['title']+': CHƯA HOÀN TẤT '+item)
                        finished+=1
                        self._emit('progress',finished,total,
                                   f'Đã xử lý {finished}/{total} chap — thành công '
                                   f'{sum(x["done"] for x in specifications)}')
            finally:
                for future in in_flight:
                    future.cancel()
                with self._futures_lock:
                    self._futures.clear()
                # Do not mark the GUI finished while Python workers still run.
                # Their remote requests are interrupted by browser.abort().
                pool.shutdown(wait=True,cancel_futures=True)
            if self.cancel.is_set():
                raise InterruptedError('Đã dừng. Bộ nhớ đệm được giữ để tiếp tục sau.')
            for spec in specifications:
                chapters=spec['chapters']; dest=spec['dest']; title=spec['title']
                done=spec['done']; failed=spec['failed']; work_dir=spec['cache']
                merged_target=spec['combined']
                if mode=='merged':
                    if done==len(chapters):
                        try:
                            pages=merge_chapter_pdfs(work_dir,chapters,merged_target,self.cancel)
                            record_merged(dest,chapters,merged_target)
                            self.log(f'{title}: PDF gộp {merged_target.name}, {pages} trang.')
                            shutil.rmtree(work_dir,ignore_errors=True)
                            root_cache=dest/'.truyensaver_cache'
                            if root_cache.is_dir() and not any(root_cache.iterdir()):
                                root_cache.rmdir()
                        except Exception as exc:
                            merged_target.unlink(missing_ok=True)
                            failed.append(f'Gộp PDF: {exc}')
                    else:
                        merged_target.unlink(missing_ok=True)
                        failed.append(f'Gộp PDF: thiếu {len(chapters)-done} chap chưa xác minh')
                        self.log(f'{title}: giữ PDF trung gian trong .truyensaver_cache để chạy lại.')
                elif mode=='mhtml':
                    write_index_formats(dest,title,chapters,'mhtml',require_ad_free=strip_ads)
                else:
                    write_index_formats(dest,title,chapters,'pdf')
                (dest/'BAO_CAO.txt').write_text('\n'.join([
                    f'TruyenSaver v{VERSION}', f'Tên truyện: {title}', f'Nguồn: {spec["url"]}',
                    f'Định dạng: {mode}',f'Đã xác minh: {done}/{len(chapters)} chap',
                    f'PDF gộp: {merged_target.name if mode=="merged" and merged_target.exists() else "không có"}',
                    'Các chương lỗi cần thử lại:']+(failed or ['Không có'])),encoding='utf-8')
                results.append(f'{title}: {done}/{len(chapters)} chap')
                self.log(f'{title}: {done}/{len(chapters)}; thư mục {dest}')
        return ' | '.join(results) + ' — xem BAO_CAO.txt để kiểm tra'

    def _close(self):
        if self.busy:
            self.stop()
            messagebox.showinfo('Đang dừng', 'Đã hủy hàng chờ và yêu cầu ngắt Chromium. '
                                'Hãy đóng ứng dụng khi trạng thái chuyển sang Đã dừng.')
            return
        self._save_settings()
        try:
            self.browser.close()
        except Exception:
            pass
        self.root.destroy()



# Self-test is safe and network-free. The GitHub Windows runner runs this on
# the *built EXE* to catch missing Chromium or packaging errors before upload.
def portable_self_test() -> int:
    from playwright.sync_api import sync_playwright
    engine=ChromiumEngine()
    try:
        executable=chromium_executable()
        if not Path(executable).exists():
            raise RuntimeError('No bundled Chromium executable')
        port=engine.ensure(visible=False)
        with sync_playwright() as driver:
            browser=driver.chromium.connect_over_cdp(f'http://127.0.0.1:{port}',timeout=15000)
            page=browser.contexts[0].new_page()
            try:
                page.set_content('<!doctype html><html><body><h1>TruyenSaver PRO</h1></body></html>')
                if page.locator('h1').inner_text()!='TruyenSaver PRO':
                    raise RuntimeError('Chromium page test failed')
                session=page.context.new_cdp_session(page)
                try:
                    blob=session.send('Page.captureSnapshot',{'format':'mhtml'})['data']
                finally:
                    session.detach()
                if 'multipart/related' not in blob:
                    raise RuntimeError('MHTML snapshot test failed')
                # End-to-end test of real Chromium -> verified snapshot -> readable PDF.
                import io,base64,tempfile
                from PIL import Image
                image=Image.new('RGB',(120,220),(183,81,105))
                buffer=io.BytesIO();image.save(buffer,format='PNG')
                data='data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode('ascii')
                page.set_content('<html><body><div class="reading-detail">'+
                                 '<img src="'+data+'"><img src="'+data+'"></div></body></html>')
                expected=wait_for_reader(page,lambda message:None,__import__('threading').Event(),max_seconds=15)
                snap,check=capture_verified_blob(page,expected,lambda message:None,strip_ads=False)
                if not check.ok:
                    raise RuntimeError('Offline MHTML validation failed')
                with tempfile.TemporaryDirectory() as dirname:
                    out=Path(dirname)/'check.pdf'
                    pdf_from_mhtml(snap,expected['urls'],out)
                    from pypdf import PdfReader
                    if not out.is_file() or len(PdfReader(str(out)).pages)<1:
                        raise RuntimeError('PDF conversion failed')
            finally:
                page.close()
        return 0
    except Exception as exc:
        try:
            p=Path(os.getenv('TEMP') or str(Path.home()))/'TruyenSaver_PRO_selftest_error.txt'
            p.write_text(str(exc),encoding='utf-8')
        except Exception:
            pass
        return 1
    finally:
        engine.close()

# Theme artwork is original and embedded for a two-file GitHub update.
MOON_FOX_BANNER_PNG_BASE64 = (
    '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'
    'AOhrq8Ng+LEOU/KLklwElSVFRXY1Nlr5YJlSKVJqR5V5OZKI/NQNKYOTUEob+9qEGUkAAAAAgCzn/N2x/pJk6JMzottQs/OSirwx'
    '+3rVp7CuRZKipjYT9LX1CkESAAAAAKAWOXFP17OkSlWRImmp5UjHjs9anxFtR2sbAPTa7Tu3qAeP7N3f7E7QefS1hYC+thDQURUC'
    'rkIppUduz2d3t4nBdG3rYUniI42qQKXlSC4Rkoi8/31vy/pWTlPbseOzt23foL9IOZKjdvW1CRVJANBnOkWKPQY80gOS0KBJrkIA'
    'uAohWMZVqM384keOrAY3L2dvoXt/7xNll1inSHo0j+LS1AZrsavQIgRJANBTyeSILAkerZ9avn5qedO7CNT6qcn1U5NVn2Xd1OS6'
    '6s/iXT3bbumLs3Zqcm31214zNbmmhS9OPduemZqcaeGLU9O2Bxl1QKklQpU2uBWeonYjAxmpoEjKsRZJRD74/uvFqqktWo7kaHpq'
    'crqFf6x6jiAJAABUhSwpR6VZUhtTkqhK99/2F6fSLKmNEVJUpXFJGyOkKMf9F4zcNklJUrMk8+zJQmFJVC0uvO6/r+3Fl45XmiLl'
    'NLUlufS1ESG1FEESAADw75WTp0RkfGRUfcqApKhXTp6u50SvnjwtkY6qVgxIerXeF6c2kz5uOf9aXXv+Xr0vjhcme17m4yocbuGL'
    'U9+es1Kb/K9XmiXlnL1ec/624R4hTYyNqRTpv/6PZ5LfTU2RNL/lSMqRFv6xcqf72lo3IEkIkgCgt5LTtZm3Db9UljSzfFnTGwmR'
    'ypIqKkpSFTc1ByUeqZ1XVDfU9hdHZUkVFSWpcqQ2pkiK2nlFdUPqsG1MkZTDPl6czKIkFffkByX5wVBOYOQrS2ooTlLlSL5SJC+F'
    'SLJYi5QqazRS1p3a3MuR+pkitR1BEgD0VzQ5IkVCFQ6fOiNkSRkqypLaHpQoFWVJ3XhxKsqS2p4iKV7ikqS2p0hKRS+OUYokkeeU'
    'HZaUXGKtoSzJY4rk5Th337VZPfj8F5/Lek7WaCQR8VWORIrUam0dEg4A8IL8CNXR92ubnbu4fmq5KlBC0vqpSV/NbsnkpV19bUnr'
    'piZ95T4NjkaKXgWP83bXTk36anZr+2ikpJmpyVjuY93X1vbRSEn6xXlgz66ya8+dObt02cSlz+3+Dz0YznTmFwd1JwOX+cVTpH7X'
    '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'
    'RK5CYII='
)

THEMES = {
    'Hsin • Moon Fox': {'bg':'#17151B','input':'#28232C','text':'#F5E8E1',
                        'muted':'#C9ABA0','accent':'#AA384A','active':'#D15262',
                        'gold':'#F1D09C','banner':'#211723','border':'#44333E'},
    'Tối': {'bg':'#151B25','input':'#242C37','text':'#EFF4FC',
             'muted':'#A9B8CA','accent':'#305F9D','active':'#437CC9',
             'gold':'#E3EFFF','banner':'#1B2737','border':'#3D4C61'},
    'Sáng': {'bg':'#F5F7FA','input':'#FFFFFF','text':'#1A2D3F',
              'muted':'#526880','accent':'#2167D9','active':'#3D7BE4',
              'gold':'#153B6B','banner':'#DFE9F6','border':'#CDD7E4'},
}


def apply_app_style(root, name='Hsin • Moon Fox'):
    pal=THEMES.get(name,THEMES['Hsin • Moon Fox'])
    style=ttk.Style(root)
    try:style.theme_use('clam')
    except tk.TclError:pass
    root.configure(background=pal['bg'])
    style.configure('.',font=('Segoe UI',10),background=pal['bg'],foreground=pal['text'])
    style.configure('TFrame',background=pal['bg'])
    style.configure('TLabel',background=pal['bg'],foreground=pal['text'])
    style.configure('Muted.TLabel',background=pal['bg'],foreground=pal['muted'],font=('Segoe UI',9))
    style.configure('Header.TLabel',background=pal['bg'],foreground=pal['gold'],font=('Segoe UI',10,'bold'))
    style.configure('TButton',padding=(10,7),background=pal['input'],foreground=pal['text'],borderwidth=0)
    style.map('TButton',background=[('active',pal['border']),('disabled',pal['input'])])
    style.configure('Primary.TButton',padding=(14,8),background=pal['accent'],foreground='#FFFFFF',
                    borderwidth=0,font=('Segoe UI',10,'bold'))
    style.map('Primary.TButton',background=[('active',pal['active']),('disabled',pal['border'])])
    style.configure('Stop.TButton',padding=(12,8),background='#6F2936',foreground='#FFFFFF')
    style.map('Stop.TButton',background=[('active','#A13747'),('disabled',pal['input'])])
    style.configure('TEntry',fieldbackground=pal['input'],foreground=pal['text'],
                    insertcolor=pal['text'],padding=(5,5),borderwidth=0)
    style.configure('TCombobox',fieldbackground=pal['input'],background=pal['input'],
                    foreground=pal['text'],arrowcolor=pal['gold'],padding=(5,4))
    style.map('TCombobox',fieldbackground=[('readonly',pal['input'])],
              foreground=[('readonly',pal['text'])])
    style.configure('TCheckbutton',background=pal['bg'],foreground=pal['text'])
    style.configure('Treeview',background=pal['input'],fieldbackground=pal['input'],foreground=pal['text'],
                    rowheight=26,borderwidth=0,font=('Segoe UI',10))
    style.configure('Treeview.Heading',background=pal['border'],foreground=pal['gold'],
                    font=('Segoe UI',9,'bold'),relief='flat')
    style.map('Treeview',background=[('selected',pal['accent'])],foreground=[('selected','#FFFFFF')])
    style.configure('Horizontal.TProgressbar',background=pal['accent'],troughcolor=pal['border'],
                    borderwidth=0,thickness=10)


if __name__ == '__main__':
    if '--self-test' in sys.argv:
        sys.exit(portable_self_test())
    window = tk.Tk()
    apply_app_style(window)
    App(window)
    window.mainloop()
