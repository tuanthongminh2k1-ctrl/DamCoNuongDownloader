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

VERSION = '3.1.0'
PDF_RENDER_VERSION = 'single-continuous-page-v3'
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
    found = CHAPTER_RE.findall((text or '') + ' ' + candidate_path.replace('-', ' ').replace('_', ' '))
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
            and entry.get('version') in ('2.0.0', '2.1.0', '2.2.0', '2.3.0', '2.4.0', '2.5.0', VERSION) and entry.get('file') == file.name
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
                urls = list(dict.fromkeys(normalize_url(x['src']) for x in confirm['images']))
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
SETTINGS = CONFIG_DIR / 'settings_v3.json'
LEGACY_SETTINGS = Path(os.getenv('APPDATA') or Path.home()) / 'TruyenSaverCocCoc' / 'settings_v2.json'


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


def plan_continuous_pdf(sizes: list[tuple[int, int]], target_width=800.,
                        max_height=13500., min_width=24.) -> list[tuple[float,float,list[int]]]:
    """Plan continuous *chapter pages*, never break through source pixels.

    Most chapters fit on ONE tall PDF page by scaling PDF coordinates (NOT
    downsampling raster artwork). For ultra-long chapters, group whole images
    into the fewest pages possible. Viewer page-gaps may remain at boundaries.
    """
    if not sizes or any(w <= 0 or h <= 0 for w,h in sizes):
        raise ValueError('Ảnh có kích thước không hợp lệ')
    ratios = [h / w for w,h in sizes]
    total_ratio = sum(ratios)
    width = min(float(target_width), float(max_height) / total_ratio)
    if width >= min_width:
        return [(width, width*total_ratio, list(range(len(sizes))))]
    # Fall back for a chapter too long even at the supported minimum width.
    out=[]; group=[]; ratio_sum=0.
    for idx,r in enumerate(ratios):
        if group and (ratio_sum + r) * min_width > max_height:
            out.append((min_width, min_width*ratio_sum, group))
            group=[]; ratio_sum=0.
        group.append(idx); ratio_sum+=r
        if ratio_sum * min_width > max_height:  # Single exceptionally tall source image.
            actual_width = max_height / ratio_sum
            out.append((actual_width, max_height, group))
            group=[]; ratio_sum=0.
    if group:
        out.append((min_width, min_width*ratio_sum, group))
    return out


def pdf_from_mhtml(blob: bytes, reader_urls: list[str], target: Path, stop=None) -> int:
    """Export borderless vertical PDF without A4 page cuts.

    Draws each source image unchanged at matching aspect ratio onto ONE very
    long PDF page per chapter in normal cases. Changing the PDF coordinate
    width does not reduce the resolution of embedded source images.
    """
    import base64
    from urllib.parse import unquote_to_bytes
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader

    if stop is not None and stop.is_set():
        raise InterruptedError('Đã dừng trước khi xử lý PDF')
    check = verify_mhtml(blob, reader_urls)
    if not check.ok:
        raise ValueError('Không xuất PDF vì ảnh MHTML chưa đủ: ' + check.explanation)
    images = _mhtml_images(blob)
    urls = [normalize_url(u) for u in reader_urls if u]  # Preserve repeated panels.
    if not urls:
        raise ValueError('Không tìm thấy ảnh truyện để xuất PDF.')
    resources=[]; sizes=[]
    for url in urls:
        if stop is not None and stop.is_set():
            raise InterruptedError('Đã dừng xuất PDF')
        raw=images.get(url)
        if raw is None and url.startswith('data:image/'):
            prefix, comma, encoded = url.partition(',')
            if comma:
                raw=(base64.b64decode(encoded, validate=True) if ';base64' in prefix
                     else unquote_to_bytes(encoded))
        if not raw:
            raise ValueError('Thiếu ảnh khi tạo PDF: '+url[:130])
        sizes.append(_pdf_probe(raw)); resources.append(raw)
    plan=plan_continuous_pdf(sizes)
    target.parent.mkdir(parents=True,exist_ok=True)
    tmp=target.with_name(target.name+'.part')
    tmp.unlink(missing_ok=True)
    try:
        pdf=canvas.Canvas(str(tmp), pagesize=(800,800),pageCompression=1)
        pdf.setTitle(target.stem)
        for width,height,indices in plan:
            if stop is not None and stop.is_set():
                raise InterruptedError('Đã dừng xuất PDF')
            pdf.setPageSize((width,height))
            y=height
            for idx in indices:
                if stop is not None and stop.is_set():
                    raise InterruptedError('Đã dừng xuất PDF')
                image,_,_=_pdf_image_reader(resources[idx])
                w,h=sizes[idx]
                draw_height=width*h/w
                y-=draw_height
                # Subpixel overlap eliminates antialiasing hairlines between
                # panels in many PDF viewers without cropping image content.
                pdf.drawImage(image,0,max(0.,y-0.008),width=width,
                              height=draw_height+0.016,mask='auto')
            pdf.showPage()
        pdf.save()
        with tmp.open('rb') as fd:
            reader=PdfReader(fd,strict=False)
            if len(reader.pages)!=len(plan):
                raise RuntimeError('PDF thiếu trang')
            import re
            drawn=sum(len(re.findall(rb'/[^\s/]+\s+Do\b',
                      p.get_contents().get_data() if p.get_contents() is not None else b''))
                      for p in reader.pages)
        if drawn<len(urls) or tmp.stat().st_size<1500:
            raise RuntimeError(f'PDF thiếu ảnh: {drawn}/{len(urls)}')
        if stop is not None and stop.is_set():
            raise InterruptedError('Đã dừng trước khi hoàn tất PDF')
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
        self.root.title(f'TruyenSaver PRO v{VERSION} — Chromium')
        self.root.geometry('960x820')
        self.root.minsize(850, 720)
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
        self._ui()
        self.links.insert('1.0', self.settings.get('links', 'https://damconuong.cool/truyen/ban-hoc'))
        root.after(120, self._drain)
        root.protocol('WM_DELETE_WINDOW', self._close)

    def _ui(self):
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill='both', expand=True)
        ttk.Label(main, text='TruyenSaver PRO — Chromium Offline', font=('Segoe UI', 19, 'bold')).pack(anchor='w')
        ttk.Label(main, text='Chạy nền • 3 nguồn truyện • PDF một trang dài cho mỗi chap • Tiếp tục tải',
                  foreground='#34526a').pack(anchor='w', pady=(3,12))
        ttk.Label(main, text='Link trang danh sách truyện (mỗi dòng một bộ truyện, hỗ trợ ba website):').pack(anchor='w')
        self.links = tk.Text(main, height=3, font=('Segoe UI', 10), wrap='word', background='#FFFFFF', foreground='#111827', relief='flat', padx=9, pady=9)
        self.links.pack(fill='x', pady=(3,9))
        range_frame = ttk.Frame(main)
        range_frame.pack(fill='x', pady=4)
        ttk.Label(range_frame, text='Từ chap:').pack(side='left')
        ttk.Entry(range_frame, textvariable=self.start_var, width=10).pack(side='left', padx=(7,22))
        ttk.Label(range_frame, text='Đến chap:').pack(side='left')
        ttk.Entry(range_frame, textvariable=self.end_var, width=10).pack(side='left', padx=(7,22))
        ttk.Label(range_frame, text='Chỉ tải các chương có trên website; không tự tạo chap thiếu.').pack(side='left')
        ttk.Label(main, text='Thư mục lưu truyện:').pack(anchor='w', pady=(9,2))
        folder = ttk.Frame(main)
        folder.pack(fill='x')
        ttk.Entry(folder, textvariable=self.folder_var).pack(side='left', fill='x', expand=True)
        ttk.Button(folder, text='Chọn thư mục', command=self._choose_folder).pack(side='left', padx=5)
        ttk.Button(folder, text='Mở thư mục', command=self._open_folder).pack(side='left')
        ttk.Button(folder, text='Đọc offline', command=self._read_offline).pack(side='left', padx=(5,0))
        fmt_frame = ttk.LabelFrame(main, text='Định dạng muốn tải', padding=(12,8))
        fmt_frame.pack(fill='x', pady=(11,5))
        ttk.Label(fmt_frame, text='Kiểu lưu:').pack(side='left', padx=(0,6))
        self.mode_box = ttk.Combobox(fmt_frame, textvariable=self.format_var, state='readonly',
                        values=('MHTML (đọc offline)', 'PDF riêng (mỗi chap)', 'PDF gộp (1 tệp)'), width=25)
        self.mode_box.pack(side='left', padx=(0,22))
        ttk.Label(fmt_frame,text='Chương tải đồng thời:').pack(side='left',padx=(0,8))
        self.workers_box=ttk.Combobox(fmt_frame,textvariable=self.workers_var,
                                      state='readonly', values=('1','2','3'), width=4)
        self.workers_box.pack(side='left')
        ttk.Label(fmt_frame,text='Mặc định 2; giảm xuống 1 nếu web chậm.').pack(side='left',padx=(12,0))
        ttk.Checkbutton(main, text='Lọc quảng cáo khi lưu MHTML (PDF chỉ lấy ảnh trong vùng đọc)',
                        variable=self.ad_free_var).pack(anchor='w', pady=(5,0))
        ttk.Label(main, text='Chromium đi kèm ứng dụng • chạy nền, không cần cài thêm trình duyệt.',
                  foreground='#34526a').pack(anchor='w', pady=(9,2))
        controls = ttk.Frame(main)
        controls.pack(fill='x', pady=(14,8))
        self.buttons = []
        for label, func in [('Mở Chromium để đăng nhập', self.login),
                            ('Quét danh sách chap', self.scan),
                            ('BẮT ĐẦU TẢI TRUYỆN', self.download)]:
            btn = ttk.Button(controls, text=label, command=func,
                             style='Primary.TButton' if func == self.download else 'TButton')
            btn.pack(side='left', padx=(0,8))
            self.buttons.append(btn)
        self.stop_button = ttk.Button(controls, text='DỪNG', command=self.stop, state='disabled')
        self.stop_button.pack(side='left')
        ttk.Label(main, textvariable=self.status_var).pack(anchor='w', pady=(5,3))
        self.progress = ttk.Progressbar(main, mode='determinate')
        self.progress.pack(fill='x', pady=(2,9))
        ttk.Label(main, text='Nhật ký tải truyện:', font=('Segoe UI',10,'bold')).pack(anchor='w')
        self.logs = scrolledtext.ScrolledText(main, height=14, font=('Consolas', 9), state='disabled', wrap='word', background='#FFFFFF', foreground='#1F2937', relief='flat', padx=9, pady=9)
        self.logs.pack(fill='both', expand=True, pady=(5,7))
        ttk.Label(main, text='PDF: một trang dài cho mỗi chap khi có thể, không cắt theo khổ A4. '
                  'Quảng cáo được lọc trước khi lưu MHTML; quảng cáo in sẵn trong ảnh truyện sẽ không thể loại tự động. '
                  'Hãy thử offline 1–2 chap trước.',
                  foreground='#755215', wraplength=900).pack(anchor='w')


    def _load_settings(self):
        for path in (SETTINGS, LEGACY_SETTINGS):
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
            'remove_ads': bool(self.ad_free_var.get())}, indent=2, ensure_ascii=False), encoding='utf-8')

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
        self._run(self._scan)

    def download(self):
        self._run(self._download)

    def _connect(self):
        from playwright.sync_api import sync_playwright
        port = self.browser.ensure(visible=False)
        return port, sync_playwright()

    def _discover(self, page, url, start, end):
        self.log('Đang quét: '+url)
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(700)
        # Some pages lazy-render the chapter list.
        try:
            anchors = page.locator('a[href]').evaluate_all(
                "els => els.map(a=>({href:a.href,text:(a.textContent||'').trim()}))")
        except Exception as ex:
            raise RuntimeError('Không đọc được danh sách chap: '+str(ex))
        chapters = [c for c in extract_chapters(anchors, url) if start <= c['number'] <= end]
        if not chapters:
            raise RuntimeError('Không tìm được chap trong khoảng chọn. Kiểm tra lại link, đăng nhập hoặc cấu trúc website.')
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
                    self._discover(page, url, start, end)
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
                        response=page.goto(url,wait_until='domcontentloaded',timeout=55000)
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
                            return True,f'Chap {number}: lưu MHTML đủ {check.matched} ảnh.'
                        pages=pdf_from_mhtml(blob,reader['urls'],target,self.cancel)
                        if self.cancel.is_set():
                            raise InterruptedError('Đã dừng trước khi xác nhận PDF')
                        write_pdf_manifest(dest,number,url,target,len(reader['urls']),pages)
                        if not pdf_completed(dest,number,url,target):
                            raise RuntimeError('Chưa xác nhận được PDF đã lưu')
                        return True,f'Chap {number}: PDF {pages} trang dài, {check.matched} ảnh.'
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        if self.cancel.is_set():
                            raise InterruptedError('Đã dừng toàn bộ tác vụ') from exc
                        self.log(f'Chap {number}: lỗi lượt {attempt+1}: {exc}')
                        if attempt==2:
                            raise
                        # Backoff prevents hammering a temporarily overloaded host.
                        for _ in range((attempt+1)*4):
                            if self.cancel.is_set():
                                raise InterruptedError('Đã dừng')
                            time.sleep(.5)
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
                        title,chapters=self._discover(scan_page,series_url,start,end)
                    except Exception as exc:
                        self.log(f'Không quét được {series_url}: {exc}')
                        results.append(series_url+': không tìm được chap')
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
                    while items and len(in_flight) < workers and not self.cancel.is_set():
                        spec,chap=items.popleft()
                        future=pool.submit(self._process_chapter,port,spec['cache'],chap,
                                           mode,strip_ads,spec['dest'])
                        in_flight[future]=(spec,chap)
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
                            self.log(spec['title']+': '+message)
                        except (InterruptedError,CancelledError):
                            self.cancel.set()
                            raise InterruptedError('Đã dừng mọi tác vụ')
                        except Exception as exc:
                            item=f'Chap {number}: {exc}'
                            spec['failed'].append(item)
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

def apply_app_style(root):
    """Lightweight native Windows-style theme, no extra UI dependencies."""
    theme=ttk.Style(root)
    try:
        theme.theme_use('clam')
    except tk.TclError:
        pass
    background='#F5F7FB'
    root.configure(background=background)
    theme.configure('.',font=('Segoe UI',10))
    theme.configure('TFrame',background=background)
    theme.configure('TLabel',background=background,foreground='#1F2937')
    theme.configure('TLabelframe',background=background,bordercolor='#D4DCE8')
    theme.configure('TLabelframe.Label',background=background,foreground='#1B3A61',
                    font=('Segoe UI',10,'bold'))
    theme.configure('TButton',background='#E4EAF3',foreground='#26364C',
                    borderwidth=0,padding=(12,8))
    theme.map('TButton',background=[('active','#CCD8E8'),('disabled','#E9EDF2')],
              foreground=[('disabled','#9BA8B9')])
    theme.configure('Primary.TButton',background='#2167D9',foreground='#FFFFFF',
                    borderwidth=0,padding=(18,9),font=('Segoe UI',10,'bold'))
    theme.map('Primary.TButton',background=[('active','#174DA7'),('disabled','#AFC1DC')],
              foreground=[('active','#FFFFFF')])
    theme.configure('TEntry',fieldbackground='#FFFFFF',padding=(5,5))
    theme.configure('TCombobox',fieldbackground='#FFFFFF',padding=(5,5))
    theme.configure('TCheckbutton',background=background,foreground='#334155')
    theme.configure('Horizontal.TProgressbar',background='#2475EC',
                    troughcolor='#DEE5F0',borderwidth=0,thickness=10)


if __name__ == '__main__':
    if '--self-test' in sys.argv:
        sys.exit(portable_self_test())
    window = tk.Tk()
    apply_app_style(window)
    App(window)
    window.mainloop()
