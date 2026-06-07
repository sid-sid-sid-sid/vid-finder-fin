"""
debug_screenshot.py

Opens Bing visual search with your frame, waits for results,
saves a screenshot AND the raw HTML so we can see exactly
what the page looks like and what we should be parsing.

Usage:
    python debug_screenshot.py frame_000018.jpg
"""

import asyncio
import sys
from pathlib import Path


async def capture(image_path: str):
    from playwright.async_api import async_playwright

    print(f"Opening Bing visual search for: {image_path}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = await context.new_page()

        # ── Bing visual search ──
        print("Navigating to Bing...")
        await page.goto(
            "https://www.bing.com/images/search?view=detailv2&iss=sbiupload",
            timeout=30000,
        )
        await page.wait_for_timeout(2000)

        print("Looking for file input...")
        # Try multiple possible selectors
        selectors = [
            'input[type="file"]',
            '#sb_imgupload',
            '[name="imageBin"]',
            'input[accept*="image"]',
        ]
        file_input = None
        for sel in selectors:
            el = page.locator(sel).first
            count = await el.count()
            print(f"  {sel}: {count} found")
            if count > 0:
                file_input = el
                break

        if file_input:
            print("Uploading image...")
            await file_input.set_input_files(image_path)
            print("Waiting for results...")
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.wait_for_timeout(3000)
        else:
            print("No file input found — saving screenshot of current page")

        # Save screenshot
        screenshot_path = "bing_results.png"
        await page.screenshot(path=screenshot_path, full_page=True)
        print(f"Screenshot saved: {screenshot_path}")

        # Save HTML
        html_path = "bing_results.html"
        html = await page.content()
        Path(html_path).write_text(html, encoding="utf-8")
        print(f"HTML saved: {html_path}  ({len(html):,} bytes)")

        # Print all anchor texts + URLs on the page
        print("\n── All anchors on page ──────────────────────────────")
        anchors = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(a => ({text: (a.innerText||'').trim().slice(0,80), href: a.href.slice(0,100)}))"
        )
        for a in anchors:
            if a["text"]:
                print(f"  {a['text']!r:50}  {a['href']}")

        print("\nKeeping browser open for 10 seconds so you can inspect...")
        await page.wait_for_timeout(10000)
        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python debug_screenshot.py <frame.jpg>")
        sys.exit(1)
    asyncio.run(capture(sys.argv[1]))