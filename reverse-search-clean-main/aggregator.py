"""
aggregator.py

Votes on the most frequent title across all frame results,
then resolves a YouTube URL for the winner.

Result sources and weights:
  exact_hit   — page title fetched directly from "Pages with this image" → short-circuits everything
  bing_id     — Bing's own query identification (one per frame)           → weight 20
  bing_visual — Bing visual matches tab                                   → weight 1, capped
  bing        — generic Bing landing links                                → weight 1, capped
"""

import re
from collections import defaultdict

# finds useless words to delete later
_NOISE = re.compile(
    r"\b(official|trailer|clip|scene|hd|4k|full movie|watch online|"
    r"streaming|download|subtitles?|blu.?ray|dvd|review|explained)\b",
    re.IGNORECASE,
)

_SKIP = {
    "skip to content", "accessibility feedback", "privacy policy",
    "terms of use", "terms", "advertise", "consumer health privacy",
    "help", "feedback", "more", "all", "search", "see more", "more videos",
    "overview", "visual matches", "pages with this image", "solve",
    "some results have been removed", "any time", "past 24 hours",
    "past week", "past month", "past year", "tools", "shopping",
    "flights", "travel", "maps", "news", "copilot", "videos", "images",
    "read all", "see all", "see more results", "load more", "show more",
    "related searches", "people also search for", "people also ask",
    "more from", "explore more", "learn more", "find out more",
    "add to cart", "buy now", "shop now", "view details", "view product",
    "in stock", "out of stock", "free shipping", "prime", "sponsored",
    "ad", "advertisement", "promoted",
}

_HIGH_TRUST = {
    "imdb", "wikipedia", "rotten tomatoes", "rottentomatoes", "metacritic",
    "letterboxd", "genius", "allmusic", "discogs", "bandcamp", "soundcloud", "spotify",
}
_MED_TRUST = {
    "netflix", "youtube", "tubi", "amazon", "hulu", "disney", "hbo", "bbc",
    "variety", "collider", "screenrant", "cinemablend", "moviefone", "justwatch",
    "rapgenius", "complex", "pitchfork", "xxlmag", "hotnewhiphop", "billboard",
}

_YOUTUBE_RE = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?[^\s\"'<>]*v=[\w-]+|youtu\.be/[\w-]+)"
)

_SOURCE_WEIGHTS = {
    "bing_id":      20.0,
    "bing_visual":   1.0,
    "bing":          1.0,
    "cache":         1.0,
    "unknown":       1.0,
}

# Max total weight any one source can contribute per frame
_SOURCE_CAP = {
    "bing_visual": 6.0,
    "bing":        6.0,
}

# gives bonus points to trusted websites
def _weight(title: str, source: str) -> float:
    if "[BING_ID]" in title:
        return 20.0
    base = _SOURCE_WEIGHTS.get(source, 1.0)
    tl = title.lower()
    for h in _HIGH_TRUST:
        if h in tl:
            return base * 2.5
    for h in _MED_TRUST:
        if h in tl:
            return base * 1.5
    return base

# cleans up messy text by removing symbols and extra spaces
def _normalise(raw: str) -> str:
    line = raw.replace("[BING_ID]", "").replace("[EXACT_HIT]", "").split("\n")[0].strip()
    t = _NOISE.sub("", line)
    t = re.sub(
        r"\s*[-–|]\s*(imdb|wikipedia|rotten tomatoes|netflix|youtube|tubi|"
        r"watch|streaming|full movie|amazon|hulu|dailymotion|moviefone|"
        r"spotify|genius|soundcloud|bandcamp).*$",
        "", t, flags=re.IGNORECASE,
    )
    t = re.sub(r"\s*[\(\[]\d{4}[\)\]]", "", t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip().lower()
    return t

# tries two different ways to search youtube for a video
def _youtube_search(title: str) -> str | None:
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                "skip_download": True, "playlistend": 1}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{title}", download=False)
            if info and info.get("entries"):
                vid_id = info["entries"][0].get("id")
                if vid_id:
                    url = f"https://www.youtube.com/watch?v={vid_id}"
                    print(f"  [YouTube] Found via yt-dlp: {url}")
                    return url
    except ImportError:
        pass
    except Exception as e:
        print(f"  [YouTube] yt-dlp failed: {e}")

    try:
        import urllib.request
        query = title.replace(" ", "+")
        req = urllib.request.Request(
            f"https://www.youtube.com/results?search_query={query}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        m = re.search(r'"videoId":"([\w-]{11})"', html)
        if m:
            url = f"https://www.youtube.com/watch?v={m.group(1)}"
            print(f"  [YouTube] Found via scrape: {url}")
            return url
    except Exception as e:
        print(f"  [YouTube] Scrape failed: {e}")

    return None



# tallies up votes to find the most likely movie title
def aggregate_results(per_frame_results: list[list]) -> dict:
    flat = [r for frame in per_frame_results for r in frame]

    if not flat:
        return {"title": None, "youtube_url": None}

    # ── Fast path: exact hit ──────────────────────────────────────────────────
    # reverse_search fetched a real page title from "Pages with this image".
    # Skip all voting — this is ground truth.
    exact_hits = [r for r in flat if getattr(r, "source", "") == "exact_hit"
                  or "[EXACT_HIT]" in r.title]
    if exact_hits:
        hit     = exact_hits[0]
        display = hit.title.replace("[EXACT_HIT]", "").strip()

        yt_match    = _YOUTUBE_RE.search(hit.url)
        youtube_url = yt_match.group(0) if yt_match else None

        if not youtube_url and display:
            print(f"  [Aggregator] Exact hit: {display!r} — searching YouTube...")
            youtube_url = _youtube_search(display)

        print(f"  [Aggregator] Result (exact): {display!r}")
        return {"title": display, "youtube_url": youtube_url}

    votes:       dict[str, float]     = defaultdict(float)
    frame_count: dict[str, set]       = defaultdict(set)
    raw_map:     dict[str, list[str]] = defaultdict(list)
    url_map:     dict[str, list[str]] = defaultdict(list)
    spent:       dict[tuple, float]   = defaultdict(float)

    skipped = 0
    for frame_idx, frame_results in enumerate(per_frame_results):
        for r in frame_results:
            source = getattr(r, "source", "unknown")

            if r.title.strip().lower() in _SKIP:
                skipped += 1
                continue

            is_strong = "[BING_ID]" in r.title or source == "bing_id"
            norm = _normalise(r.title)
            if not norm:
                skipped += 1
                continue
            if not is_strong and (len(norm.split()) < 2 or len(norm) < 8):
                skipped += 1
                continue

            w = _weight(r.title, source)
            w *= 1.0 + min(len(norm.split()) - 1, 4) * 0.05

            cap = _SOURCE_CAP.get(source)
            if cap is not None:
                key = (frame_idx, source)
                already = spent[key]
                # stops one search method from having too much voting power
                if already >= cap:
                    skipped += 1
                    continue
                w = min(w, cap - already)
                spent[key] += w

            votes[norm] += w
            frame_count[norm].add(frame_idx)
            if r.title not in raw_map[norm]:
                raw_map[norm].append(r.title)
            url_map[norm].append(r.url)

    print(f"  [Aggregator] {len(votes)} candidates, {skipped} noise entries skipped.")
    for t, s in sorted(votes.items(), key=lambda x: -x[1])[:5]:
        print(f"  [Aggregator]   {s:7.1f} pts, {len(frame_count[t])} frames — {t[:60]}")

    if not votes:
        return {"title": None, "youtube_url": None}

    winner  = max(votes, key=lambda k: votes[k])
    display = max(raw_map[winner], key=len)
    display = display.replace("[BING_ID]", "").replace("[EXACT_HIT]", "")
    display = re.sub(
        r"\s*[-–|]\s*(imdb|wikipedia|rotten tomatoes|netflix|youtube|"
        r"tubi|watch|amazon|hulu|dailymotion|moviefone|genius|spotify).*$",
        "", display, flags=re.IGNORECASE,
    ).strip()

    # resolve youtube URL
    youtube_url = None
    for url in url_map[winner]:
        m = _YOUTUBE_RE.search(url)
        if m:
            youtube_url = m.group(0)
            break
    if not youtube_url:
        for r in flat:
            m = _YOUTUBE_RE.search(r.url)
            if m:
                youtube_url = m.group(0)
                break
    if not youtube_url and display:
        youtube_url = _youtube_search(display)

    print(f"  [Aggregator] Result (voted): {display!r}")
    return {"title": display, "youtube_url": youtube_url}


def format_report(agg: dict, frames_searched: int) -> str:
    title = agg.get("title")
    yt    = agg.get("youtube_url")

    lines = [
        "",
        "┌─ RESULT " + "─" * 50,
        f"│  Title:    {title or 'not found'}",
        f"│  YouTube:  {yt or 'not found'}",
        "└" + "─" * 58,
    ]
    return "\n".join(lines)