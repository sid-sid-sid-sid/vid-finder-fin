"""
reverse_search.py

Bing Visual Search via Playwright — optimised for speed.

Fast path: "Pages with this image" finds an exact hit → fetch its <title>,
           return immediately, aggregator short-circuits.
Slow path: Use BING_ID + visual matches for voting.

Install:
    pip install playwright playwright-stealth httpx yt-dlp
    python -m playwright install chromium
"""

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote_plus

import httpx

# blueprint for saving link details
class SearchResult:
    __slots__ = ("title", "url", "source")

    def __init__(self, title: str, url: str, source: str = "unknown"):
        self.title  = title
        self.url    = url
        self.source = source

    def to_dict(self):
        return {"title": self.title, "url": self.url, "source": self.source}

    @staticmethod
    def from_dict(d):
        return SearchResult(d["title"], d["url"], d.get("source", "cache"))

    def __repr__(self):
        return f"SearchResult({self.title!r}, {self.source})"


CACHE_FILE = Path(".vf_search_cache.json")

_SKIP_TITLES = {
    "skip to content", "accessibility feedback", "privacy policy",
    "terms of use", "terms", "advertise", "consumer health privacy",
    "help", "feedback", "more", "all", "search", "see more", "more videos",
    "overview", "visual matches", "pages with this image", "solve",
    "some results have been removed", "any time", "past 24 hours",
    "past week", "past month", "past year", "tools", "shopping",
    "flights", "travel", "maps", "news", "copilot", "videos", "images",
    "read all", "see all", "load more", "show more",
}

# priority order for fetching page titles from exact-match hits
_PRIORITY_DOMAINS = [
    "youtube.com", "youtu.be",
    "genius.com", "soundcloud.com", "spotify.com",
    "bandcamp.com", "vimeo.com",
    "imdb.com", "wikipedia.org",
]

# pattern to spot youtube links in text
_YOUTUBE_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?[^\s\"'<>]*v=[\w-]+|youtu\.be/[\w-]+)"
)

_JUNK_QUERIES = {"search", "bing", "images", "results", ""}

# single shared httpx client — avoids re-creating TCP connections per fetch
_HTTP_CLIENT: httpx.AsyncClient | None = None

# reuses one internet connection to save time
async def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            follow_redirects=True,
            timeout=8,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
    return _HTTP_CLIENT

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict):
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass

# turns image file into a unique text string
def _image_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


# instantiate once, reuse across all pages
_STEALTH = None

def _get_stealth():
    global _STEALTH
    if _STEALTH is None:
        try:
            from playwright_stealth import Stealth
            _STEALTH = Stealth()
        except Exception:
            _STEALTH = False  # sentinel: stealth unavailable
    return _STEALTH

# tricks websites into thinking this is a real human
async def _apply_stealth(page):
    s = _get_stealth()
    if s:
        try:
            await s.apply_stealth_async(page)
            return
        except Exception:
            pass
    # Minimal fallback
    await page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

def _find_system_chrome() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        rf"C:\Users\{os.environ.get('USERNAME', '')}\AppData\Local\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
    ]
    return next((p for p in candidates if os.path.exists(p)), None)

# pulls the hidden title tag from a webpage
async def _fetch_page_title(url: str) -> str | None:
    try:
        client = await _get_http_client()
        resp   = await client.get(url)
        m = re.search(r"<title[^>]*>([^<]{3,300})</title>", resp.text, re.IGNORECASE)
        if not m:
            return None
        raw = (m.group(1).strip()
               .replace("&amp;", "&").replace("&quot;", '"')
               .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
        raw = re.sub(
            r"\s*[-–|]\s*(youtube|genius|soundcloud|spotify|bandcamp|vimeo|"
            r"wikipedia|imdb|bing|google|twitter|instagram|tiktok).*$",
            "", raw, flags=re.IGNORECASE,
        ).strip()
        return raw if len(raw) > 2 else None
    except Exception as e:
        print(f"  [Fetch] {url[:55]}: {e}")
        return None

# checks multiple links at the exact same time
async def _fetch_titles_parallel(urls: list[str]) -> str | None:
    """
    Fetch page titles from up to 5 URLs concurrently.
    Returns the first non-None title, prioritising known-good domains.
    """
    # Sort so YouTube/Genius come first
    def _prio(u: str) -> int:
        for i, d in enumerate(_PRIORITY_DOMAINS):
            if d in u:
                return i
        return len(_PRIORITY_DOMAINS)

    sorted_urls = sorted(urls[:5], key=_prio)

    # if the top result is already a YouTube watch URL, skip fetching entirely
    yt = _YOUTUBE_RE.search(sorted_urls[0]) if sorted_urls else None
    if yt:
        return None  # caller handles direct YT URL

    tasks = [asyncio.create_task(_fetch_page_title(u)) for u in sorted_urls]
    # Return as soon as the highest-priority task that resolves gives us a title
    for task in asyncio.as_completed(tasks):
        title = await task
        if title:
            # cancel remaining fetches — we have what we need
            for t in tasks:
                t.cancel()
            return title
    return None

# runs javascript to click a button super fast
async def _click_pivot_tab(page, label: str) -> bool:
    """
    Click a Bing pivot tab by visible label text.
    Uses a single fast JS check to find the element before committing to a click,
    avoiding Playwright's per-selector timeout overhead.
    """
    # one js call to find the element across all plausible selectors at once
    found = await page.evaluate(f"""
        (() => {{
            const frag = {json.dumps(label.lower())};
            for (const el of document.querySelectorAll('a, button')) {{
                const txt = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
                if (txt.includes(frag)) {{
                    el.click();
                    return true;
                }}
            }}
            return false;
        }})()
    """)

    if not found:
        print(f"  [Bing] Tab not found: {label!r}")
        return False

    # wait for content to update — use domcontentloaded, not networkidle
    # (networkidle stalls on Bing's background ad/tracking requests)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=6000)
        await page.wait_for_timeout(800)   # short settle for dynamic content
    except Exception:
        await page.wait_for_timeout(1200)

    print(f"  [Bing] Clicked tab: {label!r}")
    return True

# grabs text and links right from the browser memory
async def _scrape_anchors(page, source_tag: str, limit: int = 80) -> list[SearchResult]:
    anchors = await page.evaluate("""
        (() => {
            const out = [];
            const seen = new Set();
            for (const a of document.querySelectorAll('a[href]')) {
                const text = (a.innerText || a.getAttribute('aria-label') || '').trim().split('\\n')[0];
                const href = a.href || '';
                if (!text || text.length < 6 || text.length > 300) continue;
                if (!href.startsWith('http')) continue;
                if (href.includes('javascript:')) continue;
                if (href.includes('bing.com/images') || href.includes('bing.com/search')) continue;
                if (seen.has(text)) continue;
                // skip chrome nav elements
                let inChrome = false, el = a;
                while (el) {
                    const tag = (el.tagName || '').toLowerCase();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    if (tag === 'nav' || tag === 'header' || tag === 'footer' ||
                        role === 'navigation' || role === 'banner') { inChrome = true; break; }
                    el = el.parentElement;
                }
                if (inChrome) continue;
                seen.add(text);
                out.push({ text, href });
                if (out.length >= 80) break;
            }
            return out;
        })()
    """)

    results: list[SearchResult] = []
    for a in anchors[:limit]:
        text = a.get("text", "").strip()
        url  = a.get("href", "")
        if not text or text.lower() in _SKIP_TITLES:
            continue
        results.append(SearchResult(title=text, url=url, source=source_tag))
    return results

# uploads image to bing and reads the results
async def _search_one_bing(page, image_path: str, timeout: int) -> list[SearchResult]:
    results: list[SearchResult] = []

    try:
        # navigate directly to the upload endpoint
        await page.goto(
            "https://www.bing.com/images/search?view=detailv2&iss=sbiupload",
            wait_until="domcontentloaded",   # don't wait for networkidle here
            timeout=timeout * 1000,
        )

        # set the file — no sleep needed before this
        file_input = page.locator('input[type="file"]').first
        await file_input.set_input_files(image_path)

        # wait for navigation after upload — domcontentloaded only
        await page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
        await page.wait_for_timeout(1000)   # reduced from 2500ms

        landing_url = page.url

        if "REDIRERR" in landing_url or landing_url.endswith("bing.com/images?FORM=SBIRDI"):
            print(f"  [Bing] Rate-limited — skipping")
            return []

        print(f"  [Bing] Landing: {landing_url[:100]}")
        
        qs = parse_qs(urlparse(landing_url).query)
        bings_query = unquote_plus(qs["q"][0]) if "q" in qs else None
        if not bings_query:
            try:
                val = await page.input_value('input[name="q"]', timeout=2000)
                if val and len(val) > 2:
                    bings_query = val.strip()
            except Exception:
                pass
        if bings_query and bings_query.lower().strip() in _JUNK_QUERIES:
            bings_query = None
        if bings_query:
            print(f"  [Bing] Identified as: {bings_query!r}")
            results.append(SearchResult(
                title=f"{bings_query} [BING_ID]",
                url=landing_url,
                source="bing_id",
            ))

        if await _click_pivot_tab(page, "pages with"):
            exact_anchors = await _scrape_anchors(page, "bing_exact", limit=20)
            print(f"  [Bing] Exact pages: {len(exact_anchors)}")

            if exact_anchors:
                exact_urls = [r.url for r in exact_anchors]

                for url in exact_urls:
                    yt = _YOUTUBE_RE.search(url)
                    if yt:
                        yt_url = yt.group(0)
                        print(f"  [Bing] EXACT HIT (YouTube): {yt_url}")
                        return [SearchResult("[EXACT_HIT]", yt_url, "exact_hit")]

                page_title = await _fetch_titles_parallel(exact_urls)
                if page_title:
                    top_url = sorted(exact_urls[:5],
                                     key=lambda u: next(
                                         (i for i, d in enumerate(_PRIORITY_DOMAINS) if d in u),
                                         len(_PRIORITY_DOMAINS)))[0]
                    print(f"  [Bing] EXACT HIT: {page_title!r}")
                    return [SearchResult(f"{page_title} [EXACT_HIT]", top_url, "exact_hit")]

                print(f"  [Bing] Exact pages found but titles unavailable — falling back")

        if await _click_pivot_tab(page, "visual"):
            visual_results = await _scrape_anchors(page, "bing_visual", limit=60)
            print(f"  [Bing] Visual matches: {len(visual_results)}")
            results.extend(visual_results)

    except Exception as e:
        print(f"  [Bing] Error: {e}")

    return results


async def search_all_frames(
    image_paths: list[str],
    timeout: int = 45,
) -> list[list[SearchResult]]:
    """
    Search all frames via Bing Visual Search.
    Runs entirely in the background (headless).
    Stops early if any frame returns an exact hit.
    """
    cache        = _load_cache()
    results: list[list[SearchResult] | None] = [None] * len(image_paths)
    needs_search: list[int] = []

    for i, path in enumerate(image_paths):
        h = _image_hash(path)
        if h in cache:
            results[i] = [SearchResult.from_dict(d) for d in cache[h]]
            print(f"  [Cache] Frame {i+1}: {len(results[i])} results")
        else:
            needs_search.append(i)

    if not needs_search:
        return [r or [] for r in results]

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[Error] Playwright not installed. Run: pip install playwright && python -m playwright install chromium")
        return [[] for _ in image_paths]

    system_chrome = _find_system_chrome()
    new_cache_entries: dict[str, list] = {}

    async with async_playwright() as pw:
        launch_args: dict = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-background-networking",   # cuts background requests that cause networkidle delays
                "--disable-default-apps",
            ],
        }
        if system_chrome:
            launch_args["executable_path"] = system_chrome
            print(f"  [Bing] Using system Chrome: {system_chrome}")

        browser = await pw.chromium.launch(**launch_args)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            # block ad/tracking domains that cause networkidle to stall
            extra_http_headers={"DNT": "1"},
        )

        # block resource types that waste time and don't affect results
        # stops heavy images and fonts from loading to save internet speed
        await context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in {"image", "media", "font", "stylesheet"}
                   and "bing.com" not in route.request.url
                else route.continue_()
            )
        )

        for i, orig_idx in enumerate(needs_search):
            path = image_paths[orig_idx]
            print(f"\n  [Bing] Frame {orig_idx+1}/{len(image_paths)}: {Path(path).name}")

            page = await context.new_page()
            await _apply_stealth(page)
            r = await _search_one_bing(page, path, timeout)
            await page.close()

            results[orig_idx]                        = r
            new_cache_entries[_image_hash(path)]     = [x.to_dict() for x in r]
            print(f"  [Bing] Frame {orig_idx+1}: {len(r)} results")

            # Early exit on exact hit — no need to search remaining frames
            if any(res.source == "exact_hit" for res in r):
                print(f"  [Bing] Exact hit — stopping early")
                for j in range(len(image_paths)):
                    if results[j] is None:
                        results[j] = []
                break

            # inter-frame delay: 1.5s is enough to avoid rate limits
            # (was 3s before — saves 1.5s × (n_frames-1))
            if i < len(needs_search) - 1:
                await asyncio.sleep(1.5)

        await browser.close()

    # close shared HTTP client
    global _HTTP_CLIENT
    if _HTTP_CLIENT and not _HTTP_CLIENT.is_closed:
        await _HTTP_CLIENT.aclose()
        _HTTP_CLIENT = None

    # write cache once at the end, not after every frame
    if new_cache_entries:
        cache.update(new_cache_entries)
        _save_cache(cache)
        print(f"  [Cache] Saved {len(new_cache_entries)} new entries")

    return [r or [] for r in results]