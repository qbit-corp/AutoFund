# Yahoo Finance Headline & Article Scraper

A Python scraper that collects news headlines from the [Yahoo Finance](https://finance.yahoo.com/) homepage and extracts the full article content for each one, outputting everything in a structured JSON file.

> **Note:** `yfinance_scraper.py` now runs on **[Obscura](https://github.com/h4ckf0r0day/obscura)**, a Rust CDP headless browser that Playwright connects to over `chromium.connect_over_cdp()`. Playwright is still the driver; Obscura replaces bundled Chromium as the engine. The sections below describe the original Chromium-backed implementation, which is preserved as `yfinance_scraper_chrome.py`. See the "Browser Engine" section of the main [README](../README.md#browser-engine-obscura) for details.

## Why a real browser?

Yahoo Finance is a **fully JavaScript-rendered** single-page application. Traditional HTTP libraries like `requests` only receive an empty shell — the actual headlines and content are injected by JavaScript after page load. This scraper drives a real (headless) browser engine that executes all client-side JS, giving us access to the fully-rendered DOM.

---

## Prerequisites

- **Python 3.10+**
- **pip**

## Installation

Dependencies are installed once from the project root — there is no
per-package `requirements.txt`:

```bash
# From the AutoFund root
pip install -r requirements.txt

# Only needed for the Chromium fallback (yfinance_scraper_chrome.py), one-time ~150 MB
playwright install chromium

# The default engine needs Obscura instead — see the main README
```

---

## Usage

```bash
# Scrape all headlines → yahoo_finance_articles_YYYY-MM-DD_HH-MM.json (default)
python yfinance_scraper.py

# Limit to 5 articles
python yfinance_scraper.py --max 5

# Custom output file
python yfinance_scraper.py -o data/latest.json

# Visible browser window (useful for debugging)
python yfinance_scraper.py --headed

# Combine flags
python yfinance_scraper.py --max 10 -o output.json --headed
```

### CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `-o`, `--output` | `yahoo_finance_articles_YYYY-MM-DD_HH-MM.json` | Path for the output JSON file |
| `--max` | *(all found)* | Maximum number of articles to scrape |
| `--headed` | `false` | Launch the browser visibly instead of headless |

---

## Output Format

The scraper produces a single JSON file with this structure:

```json
{
  "scraped_at": "2026-04-09T19:21:09.145071+00:00",
  "source": "https://finance.yahoo.com/",
  "total_articles": 3,
  "articles": [
    {
      "title": "Palantir stock plunges after 'Big Short' investor ...",
      "url": "https://finance.yahoo.com/news/palantir-stock-plunges-...",
      "author": "Francisco Velasquez",
      "published": "2026-04-09T17:11:20.000Z",
      "content": "Full article text as a single string with paragraphs separated by double newlines...",
      "paragraphs": [
        "First paragraph text.",
        "Second paragraph text.",
        "..."
      ]
    }
  ]
}
```

### Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `scraped_at` | `string` | ISO 8601 UTC timestamp of when the scrape ran |
| `source` | `string` | The URL that was scraped (always the Yahoo Finance homepage) |
| `total_articles` | `int` | Number of articles successfully extracted |
| `articles[].title` | `string` | Article headline |
| `articles[].url` | `string` | Full URL of the article page |
| `articles[].author` | `string` | Author name(s), empty string if not found |
| `articles[].published` | `string` | ISO 8601 publish timestamp, or human-readable date string |
| `articles[].content` | `string` | Full article body as a single string (paragraphs joined by `\n\n`) |
| `articles[].paragraphs` | `string[]` | Article body split into individual paragraph strings |

---

## Internal Architecture

The scraper runs in four sequential phases inside a single Playwright browser session:

```
┌─────────────────────────────────────────────────────┐
│  Phase 1 — Load Homepage                            │
│  Launch headless Chromium → navigate to homepage     │
│  → dismiss cookie/consent banners → scroll to load   │
│    lazy content                                      │
├─────────────────────────────────────────────────────┤
│  Phase 2 — Collect Headlines                        │
│  Query DOM with CSS selectors → deduplicate URLs     │
│  → filter to article-only links                      │
├─────────────────────────────────────────────────────┤
│  Phase 3 — Scrape Articles                          │
│  For each headline URL: navigate → wait for hydrate  │
│  → extract title, author, date, body paragraphs     │
│  → 1.5s polite delay between requests                │
├─────────────────────────────────────────────────────┤
│  Phase 4 — Write JSON                               │
│  Assemble output dict → write to disk as pretty JSON │
└─────────────────────────────────────────────────────┘
```

### Module Breakdown

The entire scraper lives in `yfinance_scraper.py` and is organized as follows:

#### Constants & Configuration

| Constant | Purpose |
|----------|---------|
| `BASE_URL` | `https://finance.yahoo.com` — used for resolving relative links |
| `HEADLINE_SELECTORS` | CSS selectors used to find headline `<a>` tags on the homepage |
| `ARTICLE_BODY_SELECTORS` | CSS selectors tried in order to locate the article body container |

#### `_clean_text(text) → str`
Utility that collapses all whitespace (newlines, tabs, multiple spaces) into single spaces and strips leading/trailing blanks. Applied to every extracted text fragment.

#### `_is_article_url(href) → bool`
URL filter that determines whether a link found on the homepage is an actual article worth visiting. It rejects:
- Ticker/quote pages (`/quote/`)
- Portfolio, video, live, calendar, sector pages
- External domains (non-`finance.yahoo.com`)
- JavaScript anchors and fragment-only links

Only URLs matching `.html`/`.htm` extensions or containing `/news/` or `/articles/` path segments pass through.

#### `collect_headlines(page) → list[dict]`
Queries the homepage DOM using `HEADLINE_SELECTORS` and collects all matching `<a>` elements. For each link it:
1. Reads the `href` attribute
2. Extracts the title from `aria-label`, `title` attribute, or inner text (in that priority order)
3. Normalizes relative URLs with `urljoin`
4. Deduplicates by URL using a `seen_urls` set
5. Filters through `_is_article_url`

Returns a list of `{"title": str, "url": str}` dicts.

#### `scrape_article(page, url, fallback_title) → dict | None`
Navigates the same Playwright page to an article URL and extracts all content. Returns `None` on timeout or if no content is found. The extraction pipeline:

1. **Title extraction** — tries multiple CSS selectors in order of specificity:
   - `.caas-title-container h1` (Yahoo's Content-as-a-Service layout)
   - `.cover-title` (alternate article layout)
   - `header h1` (generic header)
   - `[data-testid='article-title']` (test-id based)
   - Any `<h1>` that isn't "Yahoo Finance" (site branding)
   - Falls back to the headline text collected from the homepage

2. **Author extraction** — looks for `.byline-attr-author` or `a.primary-link`

3. **Date extraction** — reads the `<time>` element's `datetime` attribute (ISO 8601) or its text content

4. **Body extraction** — tries `ARTICLE_BODY_SELECTORS` in order, then collects all `<p>` tags within the matched container. If no container matches, falls back to `<p>` tags inside `<article>` or `<main>`.

#### `run(output_path, max_articles, headless)`
Orchestrator function that:
1. Launches Chromium via Playwright with a desktop viewport (`1280×900`) and a Chrome user-agent string
2. Navigates to the homepage, dismisses consent banners, scrolls to trigger lazy-loading
3. Calls `collect_headlines()` to gather links
4. Iterates through headlines (optionally capped by `--max`), calling `scrape_article()` on each with a 1.5-second delay between requests
5. Writes the final JSON output

#### `main()`
CLI entry point using `argparse`. Parses `--output`, `--max`, and `--headed` flags, then calls `run()`.

---

## Selector Reference

Yahoo Finance uses dynamically generated class names (e.g. `yf-119g04z`) that change across deployments. The selectors used in this scraper were identified through **live DOM inspection in April 2026** and target the most stable class names available:

### Homepage — Headline Links
| Selector | Target |
|----------|--------|
| `a.subtle-link.titles` | Feed list items and "Latest News" headlines |
| `a.titles-link` | Hero/featured card headlines |

### Article Page — Title
| Selector | Target |
|----------|--------|
| `.caas-title-container h1` | CAAS (Content-as-a-Service) article title |
| `.cover-title` | Alternate article layout title |
| `header h1` | Generic header title |

### Article Page — Body
| Selector | Target |
|----------|--------|
| `div.caas-body` | CAAS article body (most common) |
| `div.body.yf-13ndms8` | Alternate body container |
| `article .body` | Generic fallback |

> **Note:** If Yahoo Finance redesigns their frontend, these selectors may break. The scraper will log a warning (`No headlines found`) if it can't find any links on the homepage. Use `--headed` mode to visually inspect what changed.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `No headlines found` | Yahoo changed their DOM structure | Run with `--headed` to inspect the page; update `HEADLINE_SELECTORS` |
| `Timeout loading <url>` | Slow network or Yahoo rate-limiting | Increase the timeout in `yfinance_scraper.py` (line 116) or add proxy rotation |
| Empty `author` / `published` | Yahoo uses varying layouts per article type | Expected for some article formats; the core `title` + `content` will still be present |
| `playwright._impl._errors.Error: Executable doesn't exist` | Chromium not installed | Run `playwright install chromium` |

---

## Project Structure

```
AutoFund/
├── requirements.txt                # Python dependencies (installed once, from the root)
└── scraper/
    ├── README_YFINANCE.md          # This file
    ├── yfinance_scraper.py          # Main scraper script (Obscura engine, default)
    ├── yfinance_scraper_chrome.py  # Legacy scraper using bundled Chromium
    └── yahoo_finance_articles_*.json   # Output (generated on each run, gitignored)
```

---

## License

For personal/educational use. Be aware that automated scraping of Yahoo Finance may violate their [Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html). Use responsibly, limit request frequency, and consider official financial data APIs for production workloads.

