"""
debug_search.py

Test the search pipeline on a single saved frame image.
Shows raw results from both engines before any filtering.

Usage:
    python debug_search.py frame_000018.jpg
    python debug_search.py frame_000018.jpg --no-headless
"""

import asyncio
import sys
from pathlib import Path
from reverse_search import search_all_frames_google, search_all_frames_bing
from aggregator import _is_trusted


async def debug(image_path: str, headless: bool = True):
    print(f"\n=== DEBUG SEARCH: {image_path} ===\n")
    paths = [image_path]

    print("── Google Lens (raw) ─────────────────────────────────")
    google = await search_all_frames_google(paths, timeout=40, headless=headless)
    google_results = google[0]
    if not google_results:
        print("  ✗ Zero results — likely bot detection or file input not found")
    else:
        for r in google_results:
            tag = "✓ trusted" if _is_trusted(r.url) else "✗ filtered"
            print(f"  [{tag}] {r.title[:60]}")
            print(f"           {r.url[:80]}")

    print("\n── Bing Visual Search (raw) ──────────────────────────")
    bing = await search_all_frames_bing(paths, timeout=40, headless=headless)
    bing_results = bing[0]
    if not bing_results:
        print("  ✗ Zero results")
    else:
        for r in bing_results:
            tag = "✓ trusted" if _is_trusted(r.url) else "✗ filtered"
            print(f"  [{tag}] {r.title[:60]}")
            print(f"           {r.url[:80]}")

    total = len(google_results) + len(bing_results)
    trusted = sum(1 for r in google_results + bing_results if _is_trusted(r.url))
    print(f"\n── Summary ───────────────────────────────────────────")
    print(f"  Total:   {total}  |  Trusted: {trusted}")
    if total == 0:
        print("\n  ⚠ Nothing returned. Try --no-headless to watch the browser.")
    elif trusted == 0:
        print("\n  ⚠ Results found but all from untrusted domains.")
        print("  Paste the output above so we can decide whether to expand")
        print("  the trusted domain list or fix the parser.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]

    if not args:
        print("Usage: python debug_search.py <frame.jpg> [--no-headless]")
        sys.exit(1)

    headless = "--no-headless" not in flags
    asyncio.run(debug(args[0], headless=headless))