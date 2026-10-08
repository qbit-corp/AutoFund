"""
Yahoo Finance Headline & Article Scraper
=========================================
Scrapes the Yahoo Finance homepage for news headlines, then visits each
article to extract the full content. Output is a structured JSON file.

The script starts the Obscura server (obscura serve --port 9222) itself
before scraping and terminates it when done. If Obscura is already running
on that port, it is reused and left running afterwards.

Usage:
    python yfinance_scraper_obscura.py                     # default output
    python yfinance_scraper_obscura.py -o my_output.json   # custom output
    python yfinance_scraper_obscura.py --max 5             # limit to 5 articles
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.request import urlopen

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
# Yahoo's consent service redirects some regions (including this runner) away
# from the US homepage.  These regional sites expose the same feed and do not
# require an interactive consent navigation, which Obscura cannot reliably
# complete on a long-running CDP page.
FALLBACK_HOMEPAGES = (
    "https://uk.finance.yahoo.com/",
    "https://ca.finance.yahoo.com/",
)
CONSENT_HOST = "consent.yahoo.com"
NAVIGATION_TIMEOUT_MS = 60_000

# Obscura server settings
OBSCURA_PORT = 9222
OBSCURA_READY_TIMEOUT = 30  # seconds to wait for the server to come up
# Keep the old names as aliases for callers that imported them.
OBScura_PORT = OBSCURA_PORT
OBScura_READY_TIMEOUT = OBSCURA_READY_TIMEOUT

# Selectors discovered via live DOM inspection (June 2026)
HEADLINE_SELECTORS = [
    "a.hyperlink-wrapper",           # unified feed link class (stream items)
    "a.subtle-link.titles",           # current Yahoo feed cards
    "a.titles-link",                  # featured cards
    "a.item-link",                    # regional feed cards
]
FALLBACK_HEADLINE_SELECTORS = [
    "a[href*='/news/']",              # resilient fallback for class changes
    "a[href*='/articles/']",
]

# Article body selectors (tried in order)
ARTICLE_BODY_SELECTORS = [
    "article .body",                 # current primary layout (div.body inside article)
    "div.body-wrap",                 # wrapper variant
    "div.caas-body",                 # legacy CAAS container (fallback)
    "article",                       # ultimate fallback
]
BLOCKED_RESOURCE_TYPES = {"script", "stylesheet", "image", "font", "media"}
MIN_HEADLINES_BEFORE_STOP = 10


# ── helpers ────────────────────────────────────────────────────────────────────
def _clean_text(text: str) -> str:
    """Collapse whitespace and strip surrounding blanks."""
    return re.sub(r"\s+", " ", text).strip()


def _configure_page(page) -> None:
    """Keep Yahoo's vendor bundles from wedging Obscura's JS isolate.

    The feed and article text are present in Yahoo's server-rendered HTML.
    Loading the site's analytics, video, image, and client bundles is both
    unnecessary for extraction and, in Obscura 0.2.x, can leave a CDP command
    waiting on a failed worker.  Request interception is supported by Obscura
    and still allows documents and API responses through.
    """
    def handle_route(route) -> None:
        try:
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                route.abort()
            else:
                route.continue_()
        except Exception as exc:
            log.debug("Unable to handle resource request: %s", exc)
            try:
                route.continue_()
            except Exception:
                pass

    try:
        page.route("**/*", handle_route)
    except Exception as exc:
        log.debug("Obscura request interception unavailable: %s", exc)


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
    # Must be a Yahoo Finance URL (or relative path).  The Obscura fallback
    # may use a regional host such as uk.finance.yahoo.com.
    parsed = urlparse(urljoin(BASE_URL, href))
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"}:
        return False
    if host != "finance.yahoo.com" and not host.endswith(".finance.yahoo.com"):
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
    try:
        page_base_url = page.url or BASE_URL
    except Exception:
        page_base_url = BASE_URL
    if not isinstance(page_base_url, str):
        page_base_url = BASE_URL

    def add_headline(href: str, title: str) -> None:
        if not title:
            return
        url = urldefrag(urljoin(page_base_url, href))[0]
        if not _is_article_url(url) or url in seen_urls:
            return
        seen_urls.add(url)
        headlines.append({"title": _clean_text(title), "url": url})

    def collect_from_dom(selectors: list[str]) -> None:
        for selector in selectors:
            try:
                elements = page.query_selector_all(selector)
            except Exception as exc:
                # A navigation can invalidate handles in Obscura.  One
                # selector failing should not discard links found by others.
                log.debug("Unable to query headline selector %r: %s", selector, exc)
                continue
            for el in elements:
                try:
                    href = el.get_attribute("href") or ""
                    title = (
                        el.get_attribute("aria-label")
                        or el.get_attribute("title")
                        or _clean_text(el.inner_text())
                    )
                except Exception as exc:
                    log.debug("Unable to read headline element: %s", exc)
                    continue
                add_headline(href, title)
            # Once the feed selector yielded a useful batch, avoid expensive
            # scans of alternate selectors against Obscura's very large DOM.
            if len(headlines) >= MIN_HEADLINES_BEFORE_STOP:
                break

    # Reading the complete HTML once is much cheaper than issuing one CDP
    # query per selector and one round trip per element.  Yahoo includes the
    # initial feed in server-rendered markup, so this also avoids Obscura's
    # slow/fragile JavaScript execution path.
    try:
        soup = BeautifulSoup(page.content(), "html.parser")
        for selector in HEADLINE_SELECTORS:
            for element in soup.select(selector):
                title = (
                    element.get("aria-label")
                    or element.get("title")
                    or _clean_text(element.get_text())
                )
                add_headline(element.get("href", ""), title)
            if len(headlines) >= MIN_HEADLINES_BEFORE_STOP:
                break
        if len(headlines) < MIN_HEADLINES_BEFORE_STOP:
            for selector in FALLBACK_HEADLINE_SELECTORS:
                for element in soup.select(selector):
                    add_headline(
                        element.get("href", ""),
                        element.get("aria-label")
                        or element.get("title")
                        or _clean_text(element.get_text()),
                    )
                if len(headlines) >= MIN_HEADLINES_BEFORE_STOP:
                    break
    except Exception as exc:
        log.debug("Unable to parse homepage HTML; falling back to DOM queries: %s", exc)

    if len(headlines) < MIN_HEADLINES_BEFORE_STOP:
        collect_from_dom(HEADLINE_SELECTORS)
    # Only run broad attribute selectors when the stable Yahoo classes are
    # absent.  On Obscura, scanning every anchor in Yahoo's multi-megabyte DOM
    # is substantially slower than querying the feed classes directly.
    if len(headlines) < MIN_HEADLINES_BEFORE_STOP:
        collect_from_dom(FALLBACK_HEADLINE_SELECTORS)

    log.info("Found %d unique headline links on the homepage.", len(headlines))
    return headlines


def scrape_article(context, url: str, fallback_title: str = "") -> dict | None:
    """
    Navigate to *url* and extract title + full body text.
    Returns a dict or None on failure.
    """
    page = context.new_page()
    _configure_page(page)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        # Give dynamic content a moment to hydrate
        _safe_wait(page, 500)
        if _is_consent_page(page.url):
            log.warning("Yahoo consent page returned for %s – skipping.", url)
            return None
        soup = BeautifulSoup(page.content(), "html.parser")
    except PwTimeoutError:
        log.warning("Timeout loading %s – skipping.", url)
        return None
    except Exception as exc:
        log.warning("Error loading %s: %s – skipping.", url, exc)
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass

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


def _is_consent_page(url: str) -> bool:
    """Return whether *url* is Yahoo's interactive consent endpoint."""
    return (urlparse(url).hostname or "").lower().rstrip(".") == CONSENT_HOST


def _safe_wait(page, milliseconds: int) -> None:
    """Wait without letting a closed Obscura target abort the whole run."""
    try:
        page.wait_for_timeout(milliseconds)
    except Exception as exc:
        log.debug("Page wait interrupted: %s", exc)


def _load_homepage(page, timeout: int = NAVIGATION_TIMEOUT_MS) -> str:
    """Navigate to Yahoo, falling back to a regional site if consent blocks it.

    Returns the URL that actually supplied the feed.  Obscura currently cannot
    reliably complete Yahoo's consent form: the form navigation holds the CDP
    input command until its 30-second deadline and invalidates the page's
    JavaScript execution context.  Detecting the redirect before interacting
    with the form avoids that failure mode.
    """
    candidates = (HOMEPAGE, *FALLBACK_HOMEPAGES)
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            page.goto(candidate, wait_until="domcontentloaded", timeout=timeout)
            _safe_wait(page, 500)
            current_url = page.url
        except PwTimeoutError as exc:
            last_error = exc
            log.warning("Timeout loading Yahoo homepage %s.", candidate)
            continue
        except Exception as exc:
            last_error = exc
            log.warning("Error loading Yahoo homepage %s: %s", candidate, exc)
            continue

        if _is_consent_page(current_url):
            log.warning(
                "Yahoo redirected to its consent page; trying a regional homepage."
            )
            continue
        return candidate

    if last_error:
        raise RuntimeError("Unable to load any Yahoo Finance homepage") from last_error
    raise RuntimeError("Unable to load any Yahoo Finance homepage")


# ── obscura server lifecycle ─────────────────────────────────────────────────
def _obscura_ready(port: int) -> bool:
    """Check whether the Obscura CDP endpoint is already responding."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
            if r.status != 200:
                return False
            # Reading the payload also makes sure we did not hit an unrelated
            # service that happens to be listening on the selected port.
            payload = json.load(r)
            return bool(payload.get("webSocketDebuggerUrl"))
    except Exception:
        return False


def _obscura_executable() -> str:
    """Resolve the Obscura binary used by the child server process.

    ``obscura`` is commonly installed in ``~/.local/bin`` rather than in the
    Python virtualenv.  Cron and other non-login callers may not include that
    directory on ``PATH``, so allow an explicit ``OBSCURA_BIN`` override and
    fall back to a local source checkout under ``~/obscura/dist``.  All paths
    are derived from the current user's home directory, so the same lookup
    works regardless of the account name.
    """
    home = Path.home()
    configured = os.environ.get("OBSCURA_BIN")
    candidates = [
        configured,
        shutil.which("obscura"),
        str(home / "obscura" / "dist" / "obscura"),
        str(home / ".local" / "bin" / "obscura"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and Path(candidate).stat().st_mode & 0o111:
            return str(candidate)
    # Preserve the normal shell error message if a caller has a custom PATH.
    return "obscura"


def start_obscura(port: int) -> subprocess.Popen | None:
    """
    Start the Obscura server (`obscura serve --port <port>`) and wait until
    its CDP endpoint is reachable. Returns None if it was already running.
    """
    if _obscura_ready(port):
        log.info("Obscura already running on port %d — reusing it.", port)
        return None

    log.info("Starting obscura server on port %d …", port)
    # Yahoo Finance is a large SPA.  Obscura's conservative defaults (30s
    # navigation / 5s enhancement-module budgets) can terminate a page while
    # it is still hydrating, so raise them for this scraper.  Respect values a
    # caller explicitly supplied in the environment.
    server_env = os.environ.copy()
    server_env.setdefault("OBSCURA_NAV_TIMEOUT_MS", str(NAVIGATION_TIMEOUT_MS))
    server_env.setdefault("OBSCURA_CDP_COMMAND_TIMEOUT_MS", "70000")
    server_env.setdefault("OBSCURA_SCRIPT_DEADLINE_MS", "60000")
    server_env.setdefault("OBSCURA_MODULE_BUDGET_MS", "10000")
    server_args = [_obscura_executable(), "serve", "--port", str(port)]
    if not os.environ.get("OBSCURA_VERBOSE"):
        # Yahoo's vendor bundles emit harmless worker warnings even though
        # their scripts are blocked; keep normal scraper output readable.
        server_args.append("--quiet")
    try:
        proc = subprocess.Popen(
            server_args,
            env=server_env,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "'obscura' not found on PATH. Install it or add it to PATH, "
            "or start it manually: obscura serve --port " + str(port)
        )

    deadline = time.time() + OBSCURA_READY_TIMEOUT
    while not _obscura_ready(port):
        if proc.poll() is not None:
            proc.terminate()
            raise RuntimeError("obscura exited before becoming ready")
        if time.time() > deadline:
            proc.terminate()
            raise RuntimeError(f"obscura did not become ready within {OBSCURA_READY_TIMEOUT}s")
        time.sleep(0.5)

    log.info("Obscura is ready at ws://127.0.0.1:%d", port)
    return proc


def stop_obscura(proc: subprocess.Popen | None) -> None:
    """Terminate the Obscura server we started (no-op if it was pre-existing)."""
    if proc is None:
        log.info("Obscura was pre-existing — leaving it running.")
        return
    log.info("Stopping obscura server …")
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    log.info("Obscura stopped.")


# ── main ───────────────────────────────────────────────────────────────────────
def run(output_path: str, max_articles: int | None, headless: bool, port: int = OBSCURA_PORT):
    """Run the Obscura-backed Yahoo Finance scraper."""
    obscura_proc = start_obscura(port)
    browser = None
    articles: list[dict] = []
    source_url = HOMEPAGE
    try:
        with sync_playwright() as pw:
            # Connect to the Obscura (Rust CDP-based headless browser) server
            # Use the HTTP discovery endpoint; Playwright resolves the exact
            # ``/devtools/browser`` WebSocket URL from /json/version.
            cdp_url = f"http://127.0.0.1:{port}"
            for attempt in range(3):
                try:
                    browser = pw.chromium.connect_over_cdp(cdp_url)
                    break
                except Exception as exc:
                    log.warning("CDP connect failed (attempt %d): %s", attempt + 1, exc)
                    if attempt == 2:
                        raise
                    time.sleep(1)
            # Obscura exposes one default context.  Reusing it avoids creating
            # a second isolated context that older Obscura releases cannot
            # reliably service over CDP.
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            _configure_page(page)

            # ── 1. Load homepage ──────────────────────────────────────────────
            log.info("Loading Yahoo Finance homepage …")
            source_url = _load_homepage(page)

            # ── 2. Collect headlines ──────────────────────────────────────────
            headlines = collect_headlines(page)

            # Most regional homepages contain the full feed in their initial
            # HTML.  Only scroll when the requested limit was not met; this is
            # important for Obscura because evaluating a large Yahoo SPA while
            # it is still hydrating can exceed its CDP command budget.
            needs_more = max_articles is None or len(headlines) < max_articles
            if needs_more and headlines:
                for _ in range(6):
                    try:
                        page.evaluate("window.scrollBy(0, 1000)")
                        _safe_wait(page, 1_000)
                    except Exception as exc:
                        log.warning("Unable to scroll Yahoo feed; continuing: %s", exc)
                        break
                # Scrolling may reveal additional lazy-loaded links.
                headlines = collect_headlines(page)

            if not headlines:
                raise RuntimeError(
                    "No Yahoo Finance headlines found – the site may have changed its layout"
                )

            if max_articles is not None:
                headlines = headlines[:max_articles]
                log.info("Limiting to %d articles.", max_articles)

            # ── 3. Scrape each article ────────────────────────────────────────
            total = len(headlines)
            for idx, hl in enumerate(headlines, 1):
                log.info("[%d/%d] Scraping: %s", idx, total, hl["title"][:80])
                article = scrape_article(context, hl["url"], fallback_title=hl["title"])
                if article:
                    articles.append(article)
                # polite delay between requests
                time.sleep(1.5)

            try:
                browser.close()
            except Exception as exc:
                log.debug("Error closing Playwright CDP connection: %s", exc)
            browser = None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        stop_obscura(obscura_proc)

    # ── 4. Write JSON output ──────────────────────────────────────────────
    output = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source": source_url,
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
    parser.add_argument(
        "--port",
        type=int,
        default=OBSCURA_PORT,
        help=f"Obscura CDP server port (default: {OBSCURA_PORT})",
    )
    args = parser.parse_args()
    run(args.output, args.max, headless=not args.headed, port=args.port)


if __name__ == "__main__":
    main()
