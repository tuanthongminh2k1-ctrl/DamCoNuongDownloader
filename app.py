"""TruyenSaver Chromium Offline v2.4 – portable Windows application.

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

VERSION = '2.4.0'
PDF_RENDER_VERSION = 'long-pages-no-splits-v1'
SUPPORTED_HOSTS = ('damconuong.cool', 'sayhentai.cx')
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
        raise ValueError('Chỉ hỗ trợ link HTTPS từ damconuong.cool hoặc sayhentai.cx.')
    return url


def chapter_from_anchor(text: str, link: str, series_url: str) -> float | None:
    ref, dest = urlsplit(series_url), urlsplit(urljoin(series_url, link))
    host = (ref.hostname or '').lower().removeprefix('www.')
    if host != (dest.hostname or '').lower().removeprefix('www.'):
        return None
    series_path = ref.path.rstrip('/').lower()
    candidate_path = dest.path.rstrip('/').lower()
    stem = series_path.rsplit('/', 1)[-1].removesuffix('.html')
    if host == 'sayhentai.cx':
        base = series_path.removesuffix('.html')
        suffix = candidate_path[len(base):] if candidate_path.startswith(base) else ''
        valid = suffix.startswith(('/chapter-', '/chap-', '/chuong-',
                                   '-chapter-', '-chap-', '-chuong-'))
    else:
        valid = candidate_path.startswith(series_path + '/')
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


def write_manifest_entry(folder: Path, chapter: str, url: str, file: Path, expected: int, matched: int,
                         reader_urls: list[str] | None = None):
    folder.mkdir(parents=True, exist_ok=True)
    data = read_manifest(folder)
    data[chapter] = {'url': url, 'file': file.name, 'size': file.stat().st_size,
                     'version': VERSION, 'expected': expected, 'matched': matched,
                     'reader_urls': reader_urls or [],
                     'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')}
    tmp = manifest_path(folder).with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, manifest_path(folder))


def completed(folder: Path, chapter: str, url: str, file: Path) -> bool:
    entry = read_manifest(folder).get(chapter)
    return (isinstance(entry, dict) and entry.get('url') == url
            and entry.get('version') in ('2.0.0', '2.1.0', '2.2.0', '2.3.0', VERSION) and entry.get('file') == file.name
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
 const readerEnd=Boolean(last&&(lastBottom<=innerHeight+100 || (!canScrollFurther&&top<innerHeight)));
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


def _scroll_to_last_reader_image(page) -> None:
    page.evaluate('''() => {
      let imgs=[...document.querySelectorAll('img[data-truyensaver-id]')];
      if(imgs.length) imgs[imgs.length-1].scrollIntoView({block:'end',behavior:'instant'});
    }''')


def wait_for_reader(page, log, stop, max_seconds=155) -> dict:
    '''Two-stage check: traverse every reader image, then settle the IMAGE list.

    A footer/advertisement/infinite-scroll region must not hold the reader
    hostage. This only checks images actually exposed by the website and is not
    proof that the server did not hide more pages.
    '''
    deadline = time.monotonic() + max_seconds
    last: dict = {}
    diagnostic = 'unknown'
    # Retry only when there are broken/lazy images; don't wait ~190 seconds if
    # every observed reader image is loaded and the reader itself has been seen.
    for attempt in range(3):
        if stop.is_set():
            raise InterruptedError('Đã yêu cầu dừng')
        page.evaluate('window.scrollTo(0,0)')
        page.evaluate(EAGER_JS)
        discovered = False
        reader_tail_seen = False
        last_signature = None
        stable_since = None
        last_reader_y = None
        no_move = 0
        steps = 0
        # A second pass after the initial sweep covers lazy-loaded images that
        # appear at the tail when entering the reader for the first time.
        while time.monotonic() < deadline and steps < 850:
            if stop.is_set():
                raise InterruptedError('Đã yêu cầu dừng')
            info = page.evaluate(READER_JS)
            last = info
            if not info['selector']:
                if steps > 25:
                    break
                page.wait_for_timeout(350)
                steps += 1
                continue
            discovered = True
            if info['readerEnd']:
                reader_tail_seen = True
            sig = reader_signature(info)
            if sig != last_signature:
                stable_since = time.monotonic()
            last_signature = sig
            stable_seconds = time.monotonic() - stable_since if stable_since else 0
            # A full top-to-reader-tail sweep was already performed. Do a brief
            # independent recheck of the final reader image instead of making a
            # second full sweep (which can be slow on long webtoons).
            if ready_to_archive(info, reader_tail_seen, stable_seconds):
                page.evaluate(EAGER_JS)
                _scroll_to_last_reader_image(page)
                page.wait_for_timeout(300)
                confirm = page.evaluate(READER_JS)
                if (confirm['readerEnd'] and confirm['missing'] == 0
                        and reader_signature(confirm) == sig):
                    urls = list(dict.fromkeys(normalize_url(x['src']) for x in confirm['images']))
                    if not urls:
                        raise RuntimeError('Không tìm thấy URL ảnh truyện hợp lệ.')
                    log(f"    Ảnh truyện đã đủ, ổn định: {confirm['ready']}/{confirm['total']}; "
                        f"đã kiểm tra lại đuôi vùng đọc (lượt {attempt+1}).")
                    return {'urls': urls, 'total': len(urls), 'selector': confirm['selector']}
                stable_since = None
            # Read the last image, not the ever-growing footer. At the reader's
            # last image, wait for counts and URLs to settle without scrolling.
            if info['readerEnd']:
                page.wait_for_timeout(220)
            else:
                current_y = info['scrollY']
                if current_y == last_reader_y:
                    no_move += 1
                else:
                    no_move = 0
                last_reader_y = current_y
                if no_move >= 3:
                    # Handles scroll containers and pages with huge footers.
                    _scroll_to_last_reader_image(page)
                    page.wait_for_timeout(300)
                    no_move = 0
                else:
                    page.evaluate('window.scrollBy(0,Math.max(420,Math.floor(innerHeight*0.94)))')
                    page.wait_for_timeout((85, 150, 240)[attempt])
            steps += 1
            # If the reader itself is loaded but a few images are broken, don't
            # use the whole remaining budget to stare at the same missing image.
            if info['readerEnd'] and info['missing'] > 0 and stable_seconds >= 7:
                break
        if not discovered:
            raise RuntimeError('Không tìm thấy vùng đọc truyện. Trang có thể cần đăng nhập hoặc cấu trúc website đã đổi.')
        diagnostic = (f"{last.get('ready',0)}/{last.get('total',0)} ảnh; "
                      f"đã đến cuối vùng đọc: {'có' if reader_tail_seen else 'chưa'}")
        if time.monotonic() >= deadline:
            break
        missing = [i for i in last.get('images', []) if not i['ready']]
        log(f"    Lượt {attempt+1}: {diagnostic}; "
            f"{len(missing)} ảnh chưa xong. Đang quét lại vùng đọc...")
        # Retry specific lazy/broken images, with per-image viewport activation.
        for image in missing[:150]:
            if stop.is_set():
                raise InterruptedError('Đã yêu cầu dừng')
            if time.monotonic() >= deadline:
                break
            try:
                page.evaluate('''key => {
                 const el=[...document.querySelectorAll('img')].find(i=>i.dataset.truyensaverId===key);
                 if(el) { el.scrollIntoView({block:'center'}); el.loading='eager'; }
                }''', image['key'])
                page.wait_for_timeout(270 + attempt * 230)
            except Exception:
                pass
        page.evaluate(EAGER_JS)
        _scroll_to_last_reader_image(page)
        page.wait_for_timeout(500)
    # Clear diagnostic makes 35/35-but-unstable distinct from missing 3/35.
    raise RuntimeError(f'Không thể xác nhận đủ ảnh truyện ({diagnostic}). '
                       'Không lưu chap chưa xác minh; kiểm tra trang có cần đăng nhập, '
                       'có ảnh hỏng hoặc ảnh bị ẩn hay không.')


# ---- Windows UI and browser controller ----

"""TruyenSaver Chromium Offline 2.4 (Windows).

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
SETTINGS = CONFIG_DIR / 'settings_v2_3.json'
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


def capture_verified_blob(page, expected: dict, log) -> tuple[bytes, ArchiveCheck]:
    """Take a CDP snapshot and verify its exact reader-image resources before writing."""
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


def save_atomic_bytes(data: bytes, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + '.part')
    try:
        tmp.write_bytes(data)
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


def pdf_from_mhtml(blob: bytes, reader_urls: list[str], target: Path, stop=None) -> int:
    """Export scrolling webtoon PDF, never cutting any original image.

    The images are joined top-to-bottom without borders or A4 slicing. Pages
    are long digital-only pages, assembled from whole source images. Each page
    has a maximum height within PDF viewer limits; breaks happen ONLY between
    source images, never inside artwork. An exceptionally long source image
    becomes its own page with a narrower physical page width so it still fits.
    """
    import base64
    from urllib.parse import unquote_to_bytes
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader

    check = verify_mhtml(blob, reader_urls)
    if not check.ok:
        raise ValueError('Không xuất PDF vì ảnh trong MHTML chưa đủ: ' + check.explanation)
    images = _mhtml_images(blob)
    urls = list(dict.fromkeys(normalize_url(url) for url in reader_urls if url))
    if not urls:
        raise ValueError('Không có ảnh truyện để xuất PDF.')

    # PDF points, not pixels. A long page is far better for scrolling readers.
    target_width = 800.0
    max_height = 12000.0  # Below PDF's conventional 14400pt page limit.
    pages_plan = []
    group = []
    used_height = 0.0
    image_count = 0

    for url in urls:
        if stop is not None and stop.is_set():
            raise InterruptedError('Đã dừng xuất PDF')
        resource = images.get(url)
        if resource is None and url.startswith('data:image/'):
            prefix, comma, data = url.partition(',')
            if comma:
                resource = base64.b64decode(data, validate=True) if ';base64' in prefix else unquote_to_bytes(data)
        if not resource:
            raise ValueError('PDF thiếu ảnh từ MHTML: ' + url[:130])
        # Plan layout first, without keeping hundreds of decoded images in RAM.
        px_width, px_height = _pdf_probe(resource)
        image_height = float(target_width * px_height / px_width)
        if image_height > max_height:
            if group:
                pages_plan.append((target_width, used_height, group))
                group, used_height = [], 0.0
            exact_width = max_height * px_width / px_height
            pages_plan.append((exact_width, max_height, [(resource, max_height)]))
        else:
            if group and used_height + image_height > max_height:
                pages_plan.append((target_width, used_height, group))
                group, used_height = [], 0.0
            group.append((resource, image_height))
            used_height += image_height
        image_count += 1
    if group:
        pages_plan.append((target_width, used_height, group))
    if not pages_plan or image_count != len(urls):
        raise RuntimeError(f'PDF thiếu ảnh: {image_count}/{len(urls)}')

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.part')
    temporary.unlink(missing_ok=True)
    try:
        pdf = canvas.Canvas(str(temporary), pagesize=(target_width, 1000), pageCompression=1)
        pdf.setTitle(target.stem)
        for width_pts, height_pts, items in pages_plan:
            if stop is not None and stop.is_set():
                raise InterruptedError('Đã dừng xuất PDF')
            pdf.setPageSize((width_pts, height_pts))
            top = height_pts
            for resource, draw_h in items:
                if stop is not None and stop.is_set():
                    raise InterruptedError('Đã dừng xuất PDF')
                image, _, _ = _pdf_image_reader(resource)
                top -= draw_h
                # Zero margins. Each image joins the next like a scrolling web page.
                pdf.drawImage(image, 0, max(0.0, top),
                              width=width_pts, height=draw_h, mask='auto')
            pdf.showPage()
        pdf.save()
        with temporary.open('rb') as file:
            reader = PdfReader(file, strict=False)
            count = len(reader.pages)
            # Count draw instructions, not unique embedded images: PDF correctly
            # de-duplicates identical image XObjects even when drawn many times.
            drawn = 0
            import re
            for page in reader.pages:
                contents = page.get_contents()
                raw = contents.get_data() if contents is not None else b''
                drawn += len(re.findall(rb'/[^\s/]+\s+Do\b', raw))
        if count != len(pages_plan) or drawn < image_count or temporary.stat().st_size < 1500:
            raise RuntimeError(f'PDF bị thiếu lệnh vẽ ảnh hoặc không hợp lệ: {drawn}/{image_count}')
        os.replace(temporary, target)
        return count
    finally:
        temporary.unlink(missing_ok=True)

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
            os.replace(temp, output)
        finally:
            temp.unlink(missing_ok=True)
        return total
    finally:
        writer.close()


def write_index_formats(folder: Path, title: str, chapters: list[dict], fmt: str,
                        combined: Path | None = None) -> None:
    use_mhtml = fmt in ('mhtml','both')
    use_pdf = fmt in ('pdf','both')
    entries = []
    for chap in chapters:
        number = chapter_label(chap['number'])
        links = []
        if use_mhtml and completed(folder, number, chap['url'], folder / f'Chap_{number}.mhtml'):
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
        self.root.title(f'{APP_NAME} v{VERSION}')
        self.root.geometry('960x820')
        self.root.minsize(850, 720)
        self.eventq = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.settings = self._load_settings()
        self.browser = ChromiumEngine()
        self.folder_var = tk.StringVar(value=self.settings.get('folder', str(Path.home() / 'Downloads' / 'Truyen_Offline')))
        self.start_var = tk.StringVar(value=str(self.settings.get('start', '1')))
        self.end_var = tk.StringVar(value=str(self.settings.get('end', '100')))
        self.status_var = tk.StringVar(value='Sẵn sàng – hãy thử 1–2 chap trước')
        self.format_var = tk.StringVar(value=self.settings.get('format', 'MHTML (đọc offline)'))
        self.merge_var = tk.BooleanVar(value=bool(self.settings.get('merge_pdf', True)))
        self._ui()
        self.links.insert('1.0', self.settings.get('links', 'https://damconuong.cool/truyen/ban-hoc'))
        root.after(120, self._drain)
        root.protocol('WM_DELETE_WINDOW', self._close)

    def _ui(self):
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill='both', expand=True)
        ttk.Label(main, text='TruyenSaver – Chromium Offline', font=('Segoe UI', 19, 'bold')).pack(anchor='w')
        ttk.Label(main, text='Chạy nền • MHTML offline • PDF cuộn dọc, không cắt ảnh',
                  foreground='#34526a').pack(anchor='w', pady=(3,12))
        ttk.Label(main, text='Link trang danh sách truyện (mỗi dòng một bộ truyện, hỗ trợ hai website):').pack(anchor='w')
        self.links = tk.Text(main, height=3, font=('Segoe UI', 10), wrap='word')
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
        fmt_frame = ttk.LabelFrame(main, text='Định dạng muốn tải', padding=(12,8))
        fmt_frame.pack(fill='x', pady=(11,5))
        ttk.Label(fmt_frame, text='Kiểu lưu:').pack(side='left', padx=(0,6))
        self.mode_box = ttk.Combobox(fmt_frame, textvariable=self.format_var, state='readonly',
                                     values=('MHTML (đọc offline)', 'PDF (mỗi chap)', 'MHTML + PDF'), width=23)
        self.mode_box.pack(side='left', padx=(0,18))
        self.mode_box.bind('<<ComboboxSelected>>', lambda _: self._sync_format())
        self.merge_box = ttk.Checkbutton(fmt_frame, text='Gộp toàn bộ chap thành một PDF',
                                         variable=self.merge_var)
        self.merge_box.pack(side='left')
        self._sync_format()
        ttk.Label(main, text='Chromium đi kèm ứng dụng • chạy nền, không cần cài thêm trình duyệt.',
                  foreground='#34526a').pack(anchor='w', pady=(9,2))
        controls = ttk.Frame(main)
        controls.pack(fill='x', pady=(14,8))
        self.buttons = []
        for label, func in [('Mở Chromium để đăng nhập', self.login),
                            ('Quét danh sách chap', self.scan),
                            ('BẮT ĐẦU LƯU OFFLINE', self.download)]:
            btn = ttk.Button(controls, text=label, command=func)
            btn.pack(side='left', padx=(0,8))
            self.buttons.append(btn)
        self.stop_button = ttk.Button(controls, text='DỪNG', command=self.stop, state='disabled')
        self.stop_button.pack(side='left')
        ttk.Label(main, textvariable=self.status_var).pack(anchor='w', pady=(5,3))
        self.progress = ttk.Progressbar(main, mode='determinate')
        self.progress.pack(fill='x', pady=(2,9))
        ttk.Label(main, text='Nhật ký tải truyện:', font=('Segoe UI',10,'bold')).pack(anchor='w')
        self.logs = scrolledtext.ScrolledText(main, height=14, font=('Consolas', 9), state='disabled', wrap='word')
        self.logs.pack(fill='both', expand=True, pady=(5,7))
        ttk.Label(main, text='PDF liền mạch: ghép ảnh nguyên vẹn trên trang dài, không cắt ảnh theo khổ A4. '
                  'Tùy chọn gộp chỉ xuất khi đủ tất cả chap đã chọn. Hãy thử offline 1–2 chap trước.',
                  foreground='#755215', wraplength=900).pack(anchor='w')

    def _sync_format(self):
        self.merge_box.configure(state='disabled' if self.format_var.get() == 'MHTML (đọc offline)' else 'normal')

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
            'format': self.format_var.get(), 'merge_pdf': self.merge_var.get()}, indent=2, ensure_ascii=False), encoding='utf-8')

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
        modes = {'MHTML (đọc offline)': 'mhtml', 'PDF (mỗi chap)': 'pdf', 'MHTML + PDF': 'both'}
        mode = modes.get(self.format_var.get(), 'both')
        merge = bool(self.merge_var.get()) and mode in ('pdf', 'both')
        return urls, start, end, folder, mode, merge

    def _run(self, action):
        if self.busy:
            return
        try:
            urls, start, end, folder, mode, merge = self._fields()
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
                msg = action(urls, start, end, folder, mode, merge) or msg
            except InterruptedError:
                self.log('Đã dừng theo yêu cầu. Các chap đã lưu vẫn được giữ lại.')
                msg='Đã dừng'
            except Exception as exc:
                self.log('LỖI: '+str(exc))
                msg='Có lỗi – xem nhật ký'
            finally:
                self._emit('finish', msg)
        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self.cancel.set()
        self.log('Đang yêu cầu dừng ở bước an toàn...')

    def login(self):
        def action(urls,start,end,folder,mode,merge):
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

    def _scan(self, urls, start, end, folder, mode, merge):
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

    def _download(self, urls, start, end, root, mode, merge):
        port, handle = self._connect()
        summary = []
        want_mhtml = mode in ('mhtml', 'both')
        want_pdf = mode in ('pdf', 'both')
        with handle as play:
            browser = play.chromium.connect_over_cdp(f'http://127.0.0.1:{port}', timeout=15000)
            page = browser.contexts[0].new_page()
            try:
                for url in urls:
                    if self.cancel.is_set():
                        raise InterruptedError()
                    try:
                        title, chapters = self._discover(page, url, start, end)
                    except Exception as exc:
                        self.log(f'Không quét được {url}: {exc}')
                        summary.append(f'{url}: quét thất bại')
                        continue
                    dest = root / sanitize(urlsplit(url).hostname or 'Web') / title
                    dest.mkdir(parents=True, exist_ok=True)
                    done, failed = 0, []
                    merged_file = None
                    for idx, chap in enumerate(chapters, 1):
                        if self.cancel.is_set():
                            raise InterruptedError()
                        number = chapter_label(chap['number'])
                        archive = dest / f'Chap_{number}.mhtml'
                        pdf_file = dest / f'Chap_{number}.pdf'
                        self._emit('progress', idx-1, len(chapters),
                                   f'{title}: Chap {number} ({idx}/{len(chapters)})')
                        needs_mhtml = want_mhtml and not completed(dest, number, chap['url'], archive)
                        needs_pdf = want_pdf and not pdf_completed(dest, number, chap['url'], pdf_file)
                        # Rebuild A4 PDFs as seamless PDFs from verified saved MHTML,
                        # without re-downloading any artwork, where possible.
                        if needs_pdf and archive.is_file() and completed(dest, number, chap['url'], archive):
                            try:
                                old_blob = archive.read_bytes()
                                entry = read_manifest(dest).get(number) or {}
                                image_urls = entry.get('reader_urls') or offline_reader_urls(
                                    old_blob, chap['url'], int(entry.get('expected') or 0))
                                if not image_urls or not verify_mhtml(old_blob, image_urls).ok:
                                    raise ValueError('Không khôi phục được danh sách ảnh từ MHTML cũ')
                                pages = pdf_from_mhtml(old_blob, image_urls, pdf_file, self.cancel)
                                write_pdf_manifest(dest, number, chap['url'], pdf_file,
                                                   len(image_urls), pages)
                                needs_pdf = False
                                self.log(f'Chap {number}: đã tạo PDF liền mạch trực tiếp từ MHTML cũ ({pages} trang).')
                            except InterruptedError:
                                raise
                            except Exception as exc:
                                self.log(f'Chap {number}: không dùng lại được MHTML, sẽ tải lại: {exc}')

                        if not needs_mhtml and not needs_pdf:
                            self.log(f'Chap {number}: đã có đủ định dạng, bỏ qua.')
                            done += 1
                            self._emit('progress', idx, len(chapters),
                                       f'{title}: {idx}/{len(chapters)}, đã hoàn tất {done}')
                            continue
                        try:
                            for attempt in range(2):
                                if self.cancel.is_set():
                                    raise InterruptedError()
                                try:
                                    self.log(f'Chap {number}: đang tải ảnh, lượt {attempt+1}/2...')
                                    page.goto(chap['url'], wait_until='domcontentloaded', timeout=60000)
                                    page.wait_for_timeout(500)
                                    reader = wait_for_reader(page, self.log, self.cancel, max_seconds=125)
                                    blob, check = capture_verified_blob(page, reader, self.log)
                                    if needs_mhtml:
                                        save_atomic_bytes(blob, archive)
                                        write_manifest_entry(dest, number, chap['url'], archive,
                                                             check.expected, check.matched, reader['urls'])
                                        self.log(f'    Lưu offline: {archive.name} ({check.matched} ảnh).')
                                    if needs_pdf:
                                        pages = pdf_from_mhtml(blob, reader['urls'], pdf_file, self.cancel)
                                        write_pdf_manifest(dest, number, chap['url'], pdf_file,
                                                           len(reader['urls']), pages)
                                        self.log(f'    PDF liền mạch: {pdf_file.name}, {pages} trang dài (không cắt ảnh).')
                                    break
                                except InterruptedError:
                                    raise
                                except Exception as exc:
                                    if attempt == 1:
                                        raise
                                    self.log(f'    Thử lại vì: {exc}')
                                    page.wait_for_timeout(700)
                            ok_archive = (not want_mhtml or completed(dest, number, chap['url'], archive))
                            ok_pdf = (not want_pdf or pdf_completed(dest, number, chap['url'], pdf_file))
                            if not (ok_archive and ok_pdf):
                                raise RuntimeError('Tệp đã lưu chưa qua xác nhận.')
                            done += 1
                        except InterruptedError:
                            raise
                        except Exception as exc:
                            failed.append(f'Chap {number}: {exc}')
                            self.log(f'CHƯA HOÀN TẤT CHAP {number}: {exc}')
                        self._emit('progress', idx, len(chapters),
                                   f'{title}: {idx}/{len(chapters)}, hoàn tất {done}')
                        write_index_formats(dest, title, chapters, mode)
                    if want_pdf and merge:
                        name = combined_pdf_name(chapters)
                        merged_file = dest / name
                        try:
                            total_pages = merge_chapter_pdfs(dest, chapters, merged_file, self.cancel)
                            self.log(f'Đã tạo PDF gộp: {merged_file.name} ({total_pages} trang).')
                        except InterruptedError:
                            raise
                        except Exception as exc:
                            # Do not leave a stale merged PDF looking complete.
                            merged_file.unlink(missing_ok=True)
                            merged_file = None
                            self.log(f'Không gộp PDF: {exc}')
                            failed.append(f'Gộp PDF: {exc}')
                    write_index_formats(dest, title, chapters, mode, merged_file)
                    (dest / 'BAO_CAO.txt').write_text('\n'.join([
                        f'TruyenSaver v{VERSION}', f'Truyện: {title}', f'Website: {url}',
                        f'Định dạng: {mode}', f'Hoàn tất: {done}/{len(chapters)} chap',
                        f'PDF gộp: {merged_file.name if merged_file else "không có"}',
                        'Các chap lỗi cần chạy lại:'] + (failed or ['Không có'])), encoding='utf-8')
                    summary.append(f'{title}: {done}/{len(chapters)} chap')
                    self.log(f'{title}: hoàn tất {done}/{len(chapters)}; thư mục {dest}')
            finally:
                page.close()
        return ' | '.join(summary) + ' – hãy kiểm tra tệp đầu ra'

    def _close(self):
        if self.busy:
            self.cancel.set()
            messagebox.showinfo('Đang lưu', 'Đã yêu cầu dừng. Đóng cửa sổ sau khi hoàn tất bước hiện tại.')
            return
        self._save_settings()
        try:
            self.browser.close()
        except Exception:
            pass
        self.root.destroy()


if __name__ == '__main__':
    window = tk.Tk()
    try:
        ttk.Style().theme_use('clam')
    except tk.TclError:
        pass
    App(window)
    window.mainloop()
