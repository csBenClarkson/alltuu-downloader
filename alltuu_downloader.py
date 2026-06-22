#!/usr/bin/env python3
"""
Alltuu Photo Downloader / 喔图批量原图下载器
=============================================
Organize resumable downloads from Alltuu (喔图) albums you are authorized to access.

Usage / 用法:
    python alltuu_downloader.py <album_url> [options]

Examples / 示例:
    python alltuu_downloader.py "https://m.alltuu.com/album/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/?menu=live"
    python alltuu_downloader.py "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" -o ~/Photos -w 4

Licensed under Apache License 2.0
"""

import sys
import os
import re
import json
import time
import asyncio
import argparse
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TaskProgressColumn,
)
from rich.table import Table
from rich.panel import Panel
from rich import box

# ============================================================
# Default configuration (overridable via CLI arguments)
# ============================================================
DEFAULT_WORKERS = 4
DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 3
DEFAULT_REQUEST_DELAY = 0.15
RETRY_DELAY = 2
MAX_WORKERS = 16
MAX_COMPONENT_LENGTH = 120
BROWSER_TIMEOUT = 60
SCRIPT_TIMEOUT = 300      # JS script timeout (seconds) — segment loading needs time
PAGE_LOAD_WAIT = 6         # Initial page load wait (seconds)
PAGINATION_DELAY = 1.0     # Delay between pagination requests (seconds)
PAGINATION_STALE = 5       # Stop after N consecutive rounds with no new data
PAGINATION_INIT_WAIT = 5   # Max wait for initial load (seconds)
MAX_PAGES_PER_SEG = 100    # Max pages per segment
SCROLL_STALE = 8           # Stop scrolling after N stale rounds
SCROLL_MAX_STEPS = 200     # Max scroll steps

VERSION = "3.2.0"

console = Console(force_terminal=True, legacy_windows=False)

ALLOWED_ALLTUU_HOSTS = {"alltuu.com", "www.alltuu.com", "m.alltuu.com", "v.alltuu.com"}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def parse_alltuu_url(url: str) -> str:
    """Validate an Alltuu URL (or bare album ID) and extract its album ID."""
    value = url.strip()
    if re.fullmatch(r"[a-fA-F0-9]{32}", value):
        return value.lower()

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (
        host not in ALLOWED_ALLTUU_HOSTS and not host.endswith(".alltuu.com")
    ):
        raise ValueError(
            "Expected an HTTPS Alltuu album URL or a 32-character album ID"
        )

    patterns = [
        r'/album/([a-fA-F0-9]{32})',
        r'shortName=([a-fA-F0-9]{32})',
        r'albumId=([a-fA-F0-9]{32})',
    ]
    for pat in patterns:
        m = re.search(pat, value)
        if m:
            return m.group(1).lower()
    raise ValueError("Cannot extract an album ID from the supplied Alltuu URL")


def normalize_album_url(value: str, album_id: str) -> str:
    """Return a validated URL, constructing one when a bare album ID is supplied."""
    if re.fullmatch(r"[a-fA-F0-9]{32}", value.strip()):
        return f"https://m.alltuu.com/album/{album_id}/?menu=live"
    return value.strip()


def sanitize_component(value: str, fallback: str, max_length: int = MAX_COMPONENT_LENGTH) -> str:
    """Create one portable, non-traversing filesystem path component."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback

    stem, ext = os.path.splitext(cleaned)
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
        cleaned = f"{stem}{ext}"

    if len(cleaned) > max_length:
        stem, ext = os.path.splitext(cleaned)
        keep = max(1, max_length - len(ext))
        cleaned = f"{stem[:keep]}{ext}"
    return cleaned or fallback


def resolve_output_directory(base: str, title: str, album_id: str) -> Path:
    """Resolve a title-based output directory while keeping it below the chosen base."""
    output_base = Path(base).expanduser().resolve()
    safe_title = sanitize_component(title, f"alltuu_{album_id[:8]}")
    output_dir = (output_base / safe_title).resolve()
    if os.path.commonpath((str(output_base), str(output_dir))) != str(output_base):
        raise ValueError("Album title resolved outside the selected output directory")
    return output_dir


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def worker_count(value: str) -> int:
    number = positive_int(value)
    if number > MAX_WORKERS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_WORKERS}")
    return number


def nonnegative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def extract_photos(album_id: str, album_url: str, headless: bool = True) -> dict:
    """
    Launch headless Edge, load the album page, discover all segments,
    and extract photo metadata via Vue internals + API interception.
    """
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.support.ui import WebDriverWait

    console.print("[cyan]▶ Launching headless browser...[/cyan]" if headless
                  else "[cyan]▶ Launching browser...[/cyan]")

    options = EdgeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1920,1080")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    driver = None
    try:
        driver = webdriver.Edge(options=options)
        driver.set_page_load_timeout(BROWSER_TIMEOUT)
        driver.set_script_timeout(SCRIPT_TIMEOUT)

        # --- Load page ---
        console.print("[cyan]▶ Loading album page...[/cyan]")
        driver.get(album_url)
        time.sleep(PAGE_LOAD_WAIT)

        try:
            WebDriverWait(driver, 20).until(
                lambda d: d.execute_script(
                    "return typeof window.alltuuApp !== 'undefined'"
                )
            )
            console.print("[green]  ✓ Vue app loaded[/green]")
        except Exception:
            console.print("[yellow]  ⚠ Vue app load timeout, continuing...[/yellow]")

        # --- Extract album title ---
        console.print("[cyan]▶ Extracting album info...[/cyan]")
        album_title = driver.execute_script("""
            try {
                const titleEls = document.querySelectorAll(
                    '.album-title, .title-text, [class*=albumTitle], [class*=album-title]'
                );
                for (const el of titleEls) {
                    if (el.textContent.trim()) return el.textContent.trim();
                }
                const ogTitle = document.querySelector('meta[property="og:title"]');
                if (ogTitle) return ogTitle.content;
                const dt = document.title;
                if (dt && !dt.includes('喔图')) return dt;
                const h1 = document.querySelector('h1, h2');
                if (h1) return h1.textContent.trim();
                return null;
            } catch(e) { return null; }
        """) or ""

        if not album_title:
            album_title = f"alltuu_album_{album_id[:8]}"
        album_title = album_title.replace('\n', ' ').replace('\r', '').strip()
        console.print(f"[green]  Title: {album_title}[/green]")

        # --- Install fetch/XHR interceptor ---
        # Captures photo data from all fplN API responses with deduplication
        driver.execute_script("""
            window.__capturedPhotos = [];
            window.__capturedSet = new Set();

            const origFetch = window.fetch;
            window.fetch = async function(...args) {
                const resp = await origFetch.apply(this, args);
                try {
                    const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url);
                    if (url && (url.includes('fplN') || url.includes('fplNtl'))) {
                        const clone = resp.clone();
                        clone.json().then(data => {
                            if (data && data.d && Array.isArray(data.d)) {
                                for (const item of data.d) {
                                    const key = item.id || item.ol || item.n;
                                    if (key && !window.__capturedSet.has(key)) {
                                        window.__capturedSet.add(key);
                                        window.__capturedPhotos.push({
                                            name: item.n || 'unknown.jpg',
                                            originalUrl: item.ol || '',
                                            bigUrl: item.bl || '',
                                            url1920: item.url1920 || '',
                                            thumbnailUrl: item.sl || '',
                                            width: item.w || 0,
                                            height: item.h || 0,
                                            size: item.os || '0',
                                            id: item.id || 0,
                                        });
                                    }
                                }
                            }
                        }).catch(() => {});
                    }
                } catch(e) {}
                return resp;
            };

            const origOpen = XMLHttpRequest.prototype.open;
            const origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url) {
                this._capturedUrl = url;
                return origOpen.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function() {
                if (this._capturedUrl &&
                    (this._capturedUrl.includes('fplN') || this._capturedUrl.includes('fplNtl'))) {
                    this.addEventListener('load', function() {
                        try {
                            const data = JSON.parse(this.responseText);
                            if (data && data.d && Array.isArray(data.d)) {
                                for (const item of data.d) {
                                    const key = item.id || item.ol || item.n;
                                    if (key && !window.__capturedSet.has(key)) {
                                        window.__capturedSet.add(key);
                                        window.__capturedPhotos.push({
                                            name: item.n || 'unknown.jpg',
                                            originalUrl: item.ol || '',
                                            bigUrl: item.bl || '',
                                            url1920: item.url1920 || '',
                                            thumbnailUrl: item.sl || '',
                                            width: item.w || 0,
                                            height: item.h || 0,
                                            size: item.os || '0',
                                            id: item.id || 0,
                                        });
                                    }
                                }
                            }
                        } catch(e) {}
                    });
                }
                return origSend.apply(this, arguments);
            };
        """)

        # --- Replay Performance API records (first page from initial load) ---
        console.print("[cyan]▶ Replaying initial API responses...[/cyan]")
        replay_count = driver.execute_script("""
            return new Promise(async (resolve) => {
                try {
                    const entries = performance.getEntriesByType('resource');
                    const fplNEntries = entries.filter(
                        e => e.name.includes('fplN') && !e.name.includes('fplNtl')
                    );
                    let count = 0;
                    for (const entry of fplNEntries) {
                        try {
                            const resp = await fetch(entry.name);
                            const data = await resp.json();
                            if (data && data.d && Array.isArray(data.d)) {
                                for (const item of data.d) {
                                    const key = item.id || item.ol || item.n;
                                    if (key && !window.__capturedSet.has(key)) {
                                        window.__capturedSet.add(key);
                                        window.__capturedPhotos.push({
                                            name: item.n || 'unknown.jpg',
                                            originalUrl: item.ol || '',
                                            bigUrl: item.bl || '',
                                            url1920: item.url1920 || '',
                                            thumbnailUrl: item.sl || '',
                                            width: item.w || 0,
                                            height: item.h || 0,
                                            size: item.os || '0',
                                            id: item.id || 0,
                                        });
                                        count++;
                                    }
                                }
                            }
                        } catch(e) {}
                    }
                    resolve(count);
                } catch(e) { resolve(0); }
            });
        """)
        console.print(f"[green]  Replay: {replay_count} photos[/green]")

        # --- Discover all segments (sepIdN) ---
        console.print("[cyan]▶ Discovering photo segments...[/cyan]")
        seg_data = driver.execute_script("""
            return new Promise(async (resolve) => {
                const entries = performance.getEntriesByType('resource');
                const usEntry = entries.find(e => e.name.includes('v4o/us'));
                let segments = {};
                if (usEntry) {
                    try {
                        const resp = await fetch(usEntry.name);
                        const data = await resp.json();
                        segments = data.d && data.d.s ? data.d.s : {};
                    } catch(e) {}
                }
                const el = document.querySelector('.pcAlbum');
                const vm = el && el.__vue__ ? el.__vue__ : null;
                const defaultSeg = vm ? vm.$data.nowClassify.id : null;
                const origClassify = vm ? Object.assign({}, vm.$data.nowClassify) : null;
                resolve({segments, defaultSeg, origClassify});
            });
        """)

        segments = seg_data.get('segments', {})
        default_seg = seg_data.get('defaultSeg')
        orig_classify = seg_data.get('origClassify')

        # Filter out '0' (represents "all")
        seg_ids = [k for k in segments.keys() if k != '0']

        if not seg_ids:
            console.print("[yellow]  No segments found, falling back to scroll mode[/yellow]")
            return _fallback_scroll_extract(driver, album_title, album_id)

        expected_total = sum(segments[s].get('t', 0) for s in seg_ids)
        console.print(
            f"[green]  Found {len(seg_ids)} segment(s), "
            f"expected {expected_total} photos total[/green]"
        )
        for sid in seg_ids:
            t = segments[sid].get('t', 0)
            console.print(f"    Segment {sid}: ~{t} photos")

        # --- Load all segments ---
        console.print("[cyan]▶ Loading all segments...[/cyan]")

        if not orig_classify:
            console.print("[red]  Cannot get Vue classify object, falling back to scroll[/red]")
            return _fallback_scroll_extract(driver, album_title, album_id)

        # STEP 1: Default segment — scroll the container
        default_seg_str = str(default_seg) if default_seg is not None else None
        if default_seg_str and default_seg_str in seg_ids:
            before_count = driver.execute_script(
                "return window.__capturedPhotos.length"
            )
            console.print(
                f"\n[cyan]  [Default] Scrolling segment {default_seg_str} "
                f"(~{segments[default_seg_str].get('t', 0)} expected, "
                f"{before_count} captured)...[/cyan]"
            )
            scroll_result = driver.execute_script("""
                return new Promise(async (resolve) => {
                    const container = document.querySelector('.pcAlbum-scrollList');
                    if (!container) return resolve({error: 'No container'});
                    let prevCount = window.__capturedPhotos.length;
                    let stale = 0, steps = 0;
                    while (stale < 8 && steps < 200) {
                        container.scrollTop += (container.clientHeight || 800);
                        await new Promise(r => setTimeout(r, 800));
                        steps++;
                        const cur = window.__capturedPhotos.length;
                        if (cur === prevCount) stale++;
                        else { stale = 0; prevCount = cur; }
                    }
                    resolve({
                        total: window.__capturedPhotos.length,
                        steps: steps,
                    });
                });
            """)
            if 'error' in scroll_result:
                console.print(f"[red]    Error: {scroll_result['error']}[/red]")
            else:
                new_photos = scroll_result.get('total', 0) - before_count
                steps = scroll_result.get('steps', 0)
                total_so_far = scroll_result.get('total', 0)
                console.print(
                    f"[green]    +{new_photos} photos, {steps} scroll steps "
                    f"(total: {total_so_far}/{expected_total})[/green]"
                )

        # STEP 2: Non-default segments — Vue state switch + paginate
        other_segs = [s for s in seg_ids if s != default_seg_str]

        for i, seg_id in enumerate(other_segs):
            seg_expected = segments[seg_id].get('t', 0)
            before_count = driver.execute_script(
                "return window.__capturedPhotos.length"
            )
            seg_label = f"[{i+2}/{len(seg_ids)}]" if len(seg_ids) > 1 else "[1/1]"
            console.print(
                f"\n[cyan]  {seg_label} Switching to segment {seg_id} "
                f"(~{seg_expected} expected, {before_count} captured)...[/cyan]"
            )

            # Build classify object from template
            classify_obj = dict(orig_classify)
            classify_obj['id'] = seg_id
            classify_obj['count'] = seg_expected
            classify_obj['lastCount'] = seg_expected
            classify_obj['_count'] = seg_expected

            # Vue state switch + load + paginate (with retry/scroll fallback)
            load_result = driver.execute_script("""
                return new Promise(async (resolve) => {
                    const el = document.querySelector('.pcAlbum');
                    const vm = el && el.__vue__ ? el.__vue__ : null;
                    if (!vm) return resolve({error: 'No Vue'});

                    const beforeCount = window.__capturedPhotos.length;

                    vm.$data.scrollState = false;
                    vm.$data.photoRequest = false;
                    vm.$data.scrolltop = 0;

                    vm.$set(vm.$data, 'nowClassify', arguments[0]);

                    if (vm.mixinResetList) {
                        try { vm.mixinResetList(); } catch(e) {}
                    }
                    await vm.$nextTick();
                    await new Promise(r => setTimeout(r, 1000));

                    if (vm.mixinLoadPhotos) {
                        try { vm.mixinLoadPhotos(); } catch(e) {}
                    }

                    // Adaptive wait: up to 5s, proceed early if new data arrives
                    let waited = 0;
                    const preLoadCount = window.__capturedPhotos.length;
                    while (waited < 50) {
                        await new Promise(r => setTimeout(r, 100));
                        waited++;
                        if (window.__capturedPhotos.length > preLoadCount) {
                            await new Promise(r => setTimeout(r, 1000));
                            break;
                        }
                    }

                    let afterLoadCount = window.__capturedPhotos.length;

                    // Fallback: if mixinLoadPhotos got nothing, try scrolling
                    if (afterLoadCount <= preLoadCount) {
                        const container = document.querySelector('.pcAlbum-scrollList');
                        if (container) {
                            container.scrollTop = 0;
                            await new Promise(r => setTimeout(r, 500));
                            let scrollStale = 0, scrollSteps = 0;
                            let scrollPrev = window.__capturedPhotos.length;
                            while (scrollStale < 5 && scrollSteps < 30) {
                                container.scrollTop += (container.clientHeight || 800);
                                await new Promise(r => setTimeout(r, 800));
                                scrollSteps++;
                                const cur = window.__capturedPhotos.length;
                                if (cur === scrollPrev) scrollStale++;
                                else { scrollStale = 0; scrollPrev = cur; }
                            }
                        }
                    }

                    afterLoadCount = window.__capturedPhotos.length;

                    // Paginate remaining pages
                    let pages = 0, staleRounds = 0;
                    let prevCount = afterLoadCount;

                    while (staleRounds < 5 && pages < 100) {
                        vm.$data.scrollState = false;
                        vm.$data.photoRequest = false;
                        if (vm.mixinLoadMore) {
                            try { vm.mixinLoadMore(); } catch(e) {}
                        }
                        await new Promise(r => setTimeout(r, 1000));
                        pages++;
                        const curCount = window.__capturedPhotos.length;
                        if (curCount === prevCount) staleRounds++;
                        else { staleRounds = 0; prevCount = curCount; }
                    }

                    const finalCount = window.__capturedPhotos.length;
                    resolve({
                        newPhotos: finalCount - beforeCount,
                        totalPages: pages,
                        totalCaptured: finalCount,
                    });
                });
            """, classify_obj)

            if 'error' in load_result:
                console.print(f"[red]    Error: {load_result['error']}[/red]")
            else:
                new_photos = load_result.get('newPhotos', 0)
                pages = load_result.get('totalPages', 0)
                total_so_far = load_result.get('totalCaptured', 0)
                console.print(
                    f"[green]    +{new_photos} photos, {pages} pages "
                    f"(total: {total_so_far}/{expected_total})[/green]"
                )

        # --- Extract final results ---
        photos = driver.execute_script("return window.__capturedPhotos")

        console.print(
            f"\n[bold green]✓ Extracted {len(photos)} photos "
            f"(expected {expected_total})[/bold green]"
        )

        return {
            'title': album_title,
            'total_expected': expected_total,
            'photos': photos or [],
        }

    except Exception as e:
        console.print(f"[red]Browser extraction failed: {e}[/red]")
        import traceback
        traceback.print_exc()
        return {'title': f'alltuu_{album_id[:8]}', 'total_expected': 0, 'photos': []}
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def _fallback_scroll_extract(driver, album_title, album_id):
    """Fallback: load photos by scrolling the container."""
    console.print("[yellow]  Using scroll-based fallback...[/yellow]")

    scroll_result = driver.execute_script("""
        return new Promise(async (resolve) => {
            const container = document.querySelector('.pcAlbum-scrollList');
            if (!container) {
                let prevCount = window.__capturedPhotos.length;
                let stale = 0, steps = 0;
                while (stale < 25 && steps < 200) {
                    window.scrollBy(0, window.innerHeight * 3);
                    await new Promise(r => setTimeout(r, 400));
                    steps++;
                    const cur = window.__capturedPhotos.length;
                    if (cur === prevCount) stale++;
                    else { stale = 0; prevCount = cur; }
                }
                return resolve({total: window.__capturedPhotos.length, steps, mode: 'window'});
            }
            let prevCount = window.__capturedPhotos.length;
            let stale = 0, steps = 0;
            while (stale < 8 && steps < 200) {
                container.scrollTop += (container.clientHeight || 800);
                await new Promise(r => setTimeout(r, 800));
                steps++;
                const cur = window.__capturedPhotos.length;
                if (cur === prevCount) stale++;
                else { stale = 0; prevCount = cur; }
            }
            resolve({total: window.__capturedPhotos.length, steps, mode: 'container'});
        });
    """)

    photos = driver.execute_script("return window.__capturedPhotos")
    total = scroll_result.get('total', len(photos))
    steps = scroll_result.get('steps', 0)
    mode = scroll_result.get('mode', 'unknown')
    console.print(
        f"[green]  Scroll complete: {total} photos, {steps} steps ({mode})[/green]"
    )

    return {
        'title': album_title or f'alltuu_{album_id[:8]}',
        'total_expected': 0,
        'photos': photos or [],
    }


def select_photo_url(photo: dict) -> str:
    """Choose the best available HTTPS image URL."""
    for key in ("originalUrl", "bigUrl", "url1920", "thumbnailUrl"):
        value = str(photo.get(key) or "").strip()
        if value and urlparse(value).scheme == "https":
            return value
    return ""


def prepare_download_items(photos: list) -> list:
    """Assign deterministic, collision-free target names before concurrency starts."""
    candidates = []
    counts = {}
    for index, photo in enumerate(photos, start=1):
        fallback = f"photo_{index:06d}.jpg"
        name = sanitize_component(photo.get("name") or fallback, fallback)
        if not os.path.splitext(name)[1]:
            name += ".jpg"
        candidates.append(name)
        key = name.casefold()
        counts[key] = counts.get(key, 0) + 1

    items = []
    used = set()
    for index, (photo, name) in enumerate(zip(photos, candidates), start=1):
        target = name
        if counts[name.casefold()] > 1:
            stem, ext = os.path.splitext(name)
            identity = sanitize_component(
                str(photo.get("id") or f"{index:06d}"),
                f"{index:06d}",
                max_length=40,
            )
            target = f"{stem}_{identity}{ext}"

        key = target.casefold()
        if key in used:
            stem, ext = os.path.splitext(target)
            target = f"{stem}_{index:06d}{ext}"
            key = target.casefold()
        used.add(key)
        items.append((photo, target))
    return items


def retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """Parse an HTTP Retry-After value expressed as seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            return max(0.0, retry_at.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


async def download_single(
    session: aiohttp.ClientSession,
    photo: dict,
    target_name: str,
    output_dir: Path,
    progress: Progress,
    task_id,
    semaphore: asyncio.Semaphore,
    max_retries: int,
    request_timeout: int,
    request_delay: float,
):
    """Download one photo to a temporary file and atomically finalize it."""
    url = select_photo_url(photo)
    if not url:
        return {"status": "failed", "name": target_name, "error": "No HTTPS image URL"}

    filepath = output_dir / target_name
    part_path = filepath.with_name(f"{filepath.name}.part")

    if filepath.exists() and filepath.stat().st_size > 100:
        return {"status": "skipped", "name": target_name}
    if filepath.exists():
        filepath.unlink()
    if part_path.exists():
        part_path.unlink()

    for attempt in range(max_retries):
        try:
            async with semaphore:
                if request_delay:
                    await asyncio.sleep(request_delay)
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=request_timeout),
                ) as resp:
                    if resp.status == 200:
                        content_type = resp.headers.get("Content-Type", "").lower()
                        if content_type.startswith(("text/", "application/json")):
                            raise ValueError(
                                f"Unexpected response content type: {content_type}"
                            )

                        written = 0
                        with part_path.open("wb") as handle:
                            async for chunk in resp.content.iter_chunked(256 * 1024):
                                handle.write(chunk)
                                chunk_size = len(chunk)
                                written += chunk_size
                                progress.advance(task_id, chunk_size)
                        if written <= 100:
                            raise ValueError("Downloaded file is unexpectedly small")

                        part_path.replace(filepath)
                        return {"status": "downloaded", "name": target_name}
                    if resp.status in {401, 403, 404}:
                        return {
                            "status": "failed",
                            "name": target_name,
                            "error": f"HTTP {resp.status}",
                        }
                    if attempt < max_retries - 1:
                        retry_after = retry_after_seconds(
                            resp.headers.get("Retry-After")
                        )
                        delay = retry_after
                        if delay is None:
                            delay = RETRY_DELAY * (attempt + 1)
                        await asyncio.sleep(min(delay, 60.0))
        except Exception as exc:
            if part_path.exists():
                part_path.unlink()
            if attempt < max_retries - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
            else:
                return {
                    "status": "failed",
                    "name": target_name,
                    "error": str(exc) or exc.__class__.__name__,
                }

    return {"status": "failed", "name": target_name, "error": "Retry limit reached"}


async def download_all(
    photos: list,
    output_dir: Path,
    workers: int,
    max_retries: int,
    request_timeout: int,
    request_delay: float,
):
    """Download all photos concurrently."""
    total = len(photos)
    semaphore = asyncio.Semaphore(workers)
    download_items = prepare_download_items(photos)

    total_size = sum(
        float(p.get('size', 0)) for p in photos if p.get('size')
    )
    total_size_mb = total_size / (1024 * 1024) if total_size > 0 else 0

    has_original = sum(1 for p in photos if p.get('originalUrl'))
    has_big = sum(1 for p in photos if not p.get('originalUrl') and p.get('bigUrl'))
    has_thumb = sum(
        1 for p in photos
        if not p.get('originalUrl') and not p.get('bigUrl') and p.get('thumbnailUrl')
    )

    console.print()
    console.print(
        Panel(
            f"Photos: [bold]{total}[/bold]\n"
            f"  Original: {has_original}  |  Big: {has_big}  |  Thumb: {has_thumb}\n"
            f"Est. size: [bold]{total_size_mb:.1f} MB[/bold]\n"
            f"Workers: [bold]{workers}[/bold]\n"
            f"Request delay: [bold]{request_delay:.2f}s[/bold]\n"
            f"Save to: [bold]{output_dir}[/bold]",
            title="Download Overview",
            border_style="blue",
        )
    )

    if has_thumb > 0 and has_original == 0:
        console.print(
            "[yellow]⚠ Only thumbnail URLs available; images may not be full resolution[/yellow]"
        )

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
        'Referer': 'https://m.alltuu.com/',
    }

    connector = aiohttp.TCPConnector(limit=workers, limit_per_host=workers)
    start_time = time.time()

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40),
            TaskProgressColumn(),
            TextColumn("•"),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task_total = total_size if total_size > 0 else None
            task_id = progress.add_task("Downloading", total=task_total)

            tasks = [
                download_single(
                    session, photo, target_name, output_dir,
                    progress, task_id, semaphore,
                    max_retries, request_timeout, request_delay,
                )
                for photo, target_name in download_items
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

    normalized_results = []
    for result in results:
        if isinstance(result, Exception):
            normalized_results.append(
                {"status": "failed", "name": "unknown", "error": str(result)}
            )
        else:
            normalized_results.append(result)

    downloaded = sum(1 for r in normalized_results if r["status"] == "downloaded")
    skipped = sum(1 for r in normalized_results if r["status"] == "skipped")
    failed_results = [r for r in normalized_results if r["status"] == "failed"]
    fail = len(failed_results)
    elapsed = time.time() - start_time

    actual_size = sum(
        f.stat().st_size
        for f in output_dir.iterdir()
        if f.is_file() and not f.name.endswith(".part")
    )
    actual_mb = actual_size / (1024 * 1024)

    failure_manifest = output_dir / "failed-downloads.json"
    if failed_results:
        failure_manifest.write_text(
            json.dumps(failed_results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    elif failure_manifest.exists():
        failure_manifest.unlink()

    console.print()
    table = Table(title="Download Complete", box=box.ROUNDED)
    table.add_column("Item", style="cyan")
    table.add_column("Result", style="green", justify="right")
    table.add_row("Downloaded", f"{downloaded}")
    table.add_row("Already present", f"{skipped}")
    if fail > 0:
        table.add_row("Failed", f"[red]{fail}[/red]")
        table.add_row("Failure list", str(failure_manifest))
    table.add_row("Time", f"{elapsed:.1f}s")
    table.add_row("Size", f"{actual_mb:.1f} MB")
    if elapsed > 0:
        table.add_row("Speed", f"{actual_mb / elapsed:.1f} MB/s")
    table.add_row("Saved to", str(output_dir))
    console.print(table)

    return downloaded + skipped, fail


def main():
    parser = argparse.ArgumentParser(
        description="Alltuu (喔图) Batch Photo Downloader — "
                    "organize downloads from albums you are authorized to access.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python alltuu_downloader.py "https://m.alltuu.com/album/xxxx/?menu=live"\n'
            '  python alltuu_downloader.py "https://m.alltuu.com/album/xxxx/?menu=live" '
            "-o ~/Photos -w 4\n"
            '  python alltuu_downloader.py "https://m.alltuu.com/album/xxxx/?menu=live" '
            "--no-headless\n"
        ),
    )
    parser.add_argument("url", help="Alltuu album URL")
    parser.add_argument(
        "-o", "--output", default=".",
        help="Output directory (default: current directory)",
    )
    parser.add_argument(
        "-w", "--workers", type=worker_count, default=DEFAULT_WORKERS,
        help=(
            f"Concurrent downloads, 1-{MAX_WORKERS} "
            f"(default: {DEFAULT_WORKERS})"
        ),
    )
    parser.add_argument(
        "-t", "--timeout", type=positive_int, default=DEFAULT_TIMEOUT,
        help=f"HTTP request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--retries", type=positive_int, default=DEFAULT_RETRIES,
        help=f"Max retries per photo (default: {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "--request-delay", type=nonnegative_float, default=DEFAULT_REQUEST_DELAY,
        help=(
            "Delay before each request in seconds "
            f"(default: {DEFAULT_REQUEST_DELAY})"
        ),
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="Show browser window (useful for debugging)",
    )

    args = parser.parse_args()

    console.print(
        Panel(
            f"[bold cyan]Alltuu Photo Downloader v{VERSION}[/bold cyan]\n"
            "[dim]Resumable downloads for albums you are authorized to access[/dim]",
            style="blue",
        )
    )

    album_url = args.url.strip()

    try:
        album_id = parse_alltuu_url(album_url)
        console.print(f"[green]✓ Album ID: {album_id}[/green]")
    except ValueError as e:
        console.print(f"[red]✗ {e}[/red]")
        sys.exit(1)

    album_url = normalize_album_url(album_url, album_id)

    # --- Extract photos ---
    result = extract_photos(album_id, album_url, headless=not args.no_headless)
    photos = result.get('photos', [])
    title = result.get('title', f'alltuu_{album_id[:8]}')

    if not photos:
        console.print("[red]✗ No photos extracted[/red]")
        console.print(
            "[yellow]Possible causes: password-protected album, "
            "expired link, or network issue[/yellow]"
        )
        sys.exit(1)

    # Filter photos with valid URLs
    valid_photos = [
        p for p in photos
        if select_photo_url(p)
    ]
    console.print(
        f"[green]✓ {len(valid_photos)} valid photos "
        f"(from {len(photos)} records)[/green]"
    )

    # --- Create output directory ---
    output_dir = resolve_output_directory(args.output, title, album_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Download ---
    success, fail = asyncio.run(
        download_all(
            valid_photos, output_dir,
            workers=args.workers,
            max_retries=args.retries,
            request_timeout=args.timeout,
            request_delay=args.request_delay,
        )
    )

    if success > 0:
        console.print(
            f"\n[bold green]✓ Done! {success} photos saved to:[/bold green]\n"
            f"  [bold]{output_dir}[/bold]"
        )
    if fail > 0:
        console.print(
            f"[yellow]⚠ {fail} photo(s) failed. "
            f"Re-run to retry (already downloaded files are skipped).[/yellow]"
        )


if __name__ == '__main__':
    main()
