"""
Yahoo Finance Headline & Article Scraper
=========================================
Scrapes the Yahoo Finance homepage for news headlines, then visits each
article to extract the full content. Output is a structured JSON file.

Usage:
    python scraper.py                     # default: output to yahoo_finance_articles_YYYY-MM-DD_HH-MM.json
    python scraper.py -o my_output.json   # custom output path
    python scraper.py --max 5             # limit to 5 articles
    python scraper.py --headed            # run with visible browser (debug)
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeoutError

# ── logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("yf-scraper")

BASE_URL = "https://finance.yahoo.com"
HOMEPAGE = f"{BASE_URL}/"

# Selectors discovered via live DOM inspection (June 2026)
HEADLINE_SELECTORS = [
    "a.hyperlink-wrapper",           # unified feed link class (stream items)
]

# Article body selectors (tried in order)
ARTICLE_BODY_SELECTORS = [
    "article .body",                 # current primary layout (div.body inside article)
    "div.body-wrap",                 # wrapper variant
    "div.caas-body",                 # legacy CAAS container (fallback)
    "article",                       # ultimate fallback
]


# ── helpers ────────────────────────────────────────────────────────────────────
def _clean_text(text: str) -> str:
    """Collapse whitespace and strip surrounding blanks."""
    return re.sub(r"\s+", " ", text).strip()


def _is_article_url(href: str) -> bool:
    """Return True if the href looks like a Yahoo Finance article page."""
    if not href:
        return False
    # Filter out tickers, portfolio links, pure listing pages, external sites, etc.
    skip_patterns = (
        "/quote/", "/portfolio/", "/videos/",
        "/lookup/", "/calendars/",
        "javascript:", "#",
    )
    if any(pat in href for pat in skip_patterns):
        return False
    # Must be a finance.yahoo.com URL (or relative path)
    if href.startswith("http") and "finance.yahoo.com" not in href:
        return False
    # Accept any URL that looks like an article, live-blog, video, or sector article
    return bool(
        re.search(r"\.(html|htm)$", href)
        or "/news/" in href
        or "/articles/" in href
        or "/article/" in href
        or "/video/" in href
        or "/live/" in href
    )


def collect_headlines(page) -> list[dict]:
    """
    From the current (homepage) page, gather all unique headline links.
    Returns a list of dicts with keys: title, url.
    """
    seen_urls: set[str] = set()
    headlines: list[dict] = []

    for selector in HEADLINE_SELECTORS:
        elements = page.query_selector_all(selector)
        for el in elements:
            href = el.get_attribute("href") or ""
            title = (
                el.get_attribute("aria-label")
                or el.get_attribute("title")
                or _clean_text(el.inner_text())
            )
            if not title:
                continue

            # Normalise URL
            url = urljoin(BASE_URL, href)
            if not _is_article_url(url):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            headlines.append({"title": _clean_text(title), "url": url})

    log.info("Found %d unique headline links on the homepage.", len(headlines))
    return headlines


def scrape_article(context, url: str, fallback_title: str = "") -> dict | None:
    """
    Navigate to *url* and extract title + full body text.
    Returns a dict or None on failure.
    """
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Give dynamic content a moment to hydrate
        page.wait_for_timeout(2_000)
        soup = BeautifulSoup(page.content(), "html.parser")
    except PwTimeoutError:
        log.warning("Timeout loading %s – skipping.", url)
        return None
    except Exception as exc:
        log.warning("Error loading %s: %s – skipping.", url, exc)
        return None
    finally:
        page.close()

    # ── title ──────────────────────────────────────────────────────────────
    # Try article-specific title selectors first (the generic h1 is often
    # just the "Yahoo Finance" site branding)
    title = ""
    for title_sel in [
        ".cover-title",                  # current primary title selector
        ".caas-title-container h1",      # legacy CAAS layout
        "[data-testid='article-title']",
        "header h1",
    ]:
        el = soup.select_one(title_sel)
        if el:
            candidate = _clean_text(el.get_text())
            if candidate and candidate.lower() != "yahoo finance":
                title = candidate
                break

    # Fallback: try any h1 that isn't the site name
    if not title:
        for h1 in soup.find_all("h1"):
            candidate = _clean_text(h1.get_text())
            if candidate and candidate.lower() != "yahoo finance":
                title = candidate
                break

    # Last resort: use the headline text collected from the homepage
    if not title:
        title = fallback_title

    # ── author & date ──────────────────────────────────────────────────────
    author_el = soup.select_one(".byline-attr-author, a.primary-link")
    author = _clean_text(author_el.get_text()) if author_el else ""

    time_el = soup.select_one("time, .byline-attr-date-time")
    published = ""
    if time_el:
        published = time_el.get("datetime") or _clean_text(time_el.get_text())

    # ── body paragraphs ────────────────────────────────────────────────────
    body_container = None
    for sel in ARTICLE_BODY_SELECTORS:
        body_container = soup.select_one(sel)
        if body_container:
            break

    paragraphs: list[str] = []
    if body_container:
        for p in body_container.find_all("p"):
            text = _clean_text(p.get_text())
            if text:
                paragraphs.append(text)

    if not paragraphs:
        # Fallback: grab all <p> inside <article> or main content area
        article_tag = soup.select_one("article") or soup.select_one("main")
        if article_tag:
            for p in article_tag.find_all("p"):
                text = _clean_text(p.get_text())
                if text:
                    paragraphs.append(text)

    if not title and not paragraphs:
        log.warning("No content extracted from %s – skipping.", url)
        return None

    return {
        "title": title,
        "url": url,
        "author": author,
        "published": published,
        "content": "\n\n".join(paragraphs),
        "paragraphs": paragraphs,
    }


# ── main ───────────────────────────────────────────────────────────────────────
def run(output_path: str, max_articles: int | None, headless: bool):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # ── 1. Load homepage ──────────────────────────────────────────────
        log.info("Loading Yahoo Finance homepage …")
        page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(3_000)

        # Dismiss cookie / consent banners if present
        for consent_btn in [
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Agree')",
            "button:has-text('OK')",
            "button[name='agree']",
        ]:
            try:
                btn = page.query_selector(consent_btn)
                if btn and btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(1_000)
                    break
            except Exception:
                pass

        # Scroll down aggressively to load more lazy-loaded stream items
        for _ in range(6):
            page.evaluate("window.scrollBy(0, 1000)")
            page.wait_for_timeout(1_000)

        # ── 2. Collect headlines ──────────────────────────────────────────
        headlines = collect_headlines(page)
        if not headlines:
            log.error("No headlines found – Yahoo may have changed their layout.")
            browser.close()
            sys.exit(1)

        if max_articles:
            headlines = headlines[:max_articles]
            log.info("Limiting to %d articles.", max_articles)

        # ── 3. Scrape each article ────────────────────────────────────────
        articles: list[dict] = []
        total = len(headlines)
        for idx, hl in enumerate(headlines, 1):
            log.info("[%d/%d] Scraping: %s", idx, total, hl["title"][:80])
            article = scrape_article(context, hl["url"], fallback_title=hl["title"])
            if article:
                articles.append(article)
            # polite delay between requests
            time.sleep(1.5)

        browser.close()

    # ── 4. Write JSON output ──────────────────────────────────────────────
    output = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source": HOMEPAGE,
        "total_articles": len(articles),
        "articles": articles,
    }

    out = Path(output_path)
    out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("✓ Saved %d articles to %s", len(articles), out.resolve())


# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    default_filename = f"yahoo_finance_articles_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.json"
    parser = argparse.ArgumentParser(
        description="Scrape Yahoo Finance homepage headlines & article content."
    )
    parser.add_argument(
        "-o", "--output",
        default=default_filename,
        help=f"Output JSON file path (default: {default_filename})",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Maximum number of articles to scrape (default: all found)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run the browser in headed (visible) mode for debugging",
    )
    args = parser.parse_args()
    run(args.output, args.max, headless=not args.headed)


if __name__ == "__main__":
    main()
