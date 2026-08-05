"""
Amazon Best Sellers scraper — cloud edition v2.

Scrapes many categories via ScraperAPI, extracts richer product data, and
computes rank movement by diffing against the previous run.

Writes:
    data/latest.json                  — current snapshot (what the dashboard reads)
    data/history/YYYY-MM-DDTHH.json   — timestamped archive of each run

Environment variables:
    SCRAPERAPI_KEY   — required
    ONLY_CATEGORIES  — optional, comma-separated slugs to scrape just a subset
"""

import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

API_KEY = os.environ.get("SCRAPERAPI_KEY", "")
if not API_KEY:
    print("ERROR: SCRAPERAPI_KEY environment variable not set", file=sys.stderr)
    sys.exit(1)

DATA_DIR = Path("data")
HISTORY_DIR = DATA_DIR / "history"
LATEST = DATA_DIR / "latest.json"

# How many products to keep per category.
TOP_N = 30

# Parallel requests. ScraperAPI free tier allows modest concurrency; 4 is safe.
WORKERS = 4

# ─────────────────────────────────────────────────────────────────────────────
# Categories. Grouped for the dashboard's sidebar. Add / remove freely —
# the dashboard builds its UI from whatever is here.
# ─────────────────────────────────────────────────────────────────────────────
CATEGORIES = {
    # ── Greeting cards & stationery ──
    "cards-birthday": ("Birthday cards", "Cards & stationery",
                       "https://www.amazon.co.uk/Best-Sellers/zgbs/officeproduct/332146031"),
    "cards-seasonal": ("Seasonal cards", "Cards & stationery",
                       "https://www.amazon.co.uk/Best-Sellers/zgbs/officeproduct/332149031"),
    "cards-all": ("All greeting cards", "Cards & stationery",
                  "https://www.amazon.co.uk/Best-Sellers/zgbs/officeproduct/217147031"),
    "office-all": ("Office products", "Cards & stationery",
                   "https://www.amazon.co.uk/Best-Sellers/zgbs/officeproduct"),
    "stationery": ("Stationery", "Cards & stationery",
                   "https://www.amazon.co.uk/Best-Sellers/zgbs/officeproduct/129837031"),

    # ── Home & living ──
    "home-kitchen": ("Home & kitchen", "Home & living",
                     "https://www.amazon.co.uk/Best-Sellers/zgbs/kitchen"),
    "furniture": ("Furniture", "Home & living",
                  "https://www.amazon.co.uk/Best-Sellers/zgbs/kitchen/11711651"),
    "garden": ("Garden & outdoors", "Home & living",
               "https://www.amazon.co.uk/Best-Sellers/zgbs/garden"),
    "diy": ("DIY & tools", "Home & living",
            "https://www.amazon.co.uk/Best-Sellers/zgbs/diy"),
    "lighting": ("Lighting", "Home & living",
                 "https://www.amazon.co.uk/Best-Sellers/zgbs/lighting"),

    # ── Gifts & celebration ──
    "toys": ("Toys & games", "Gifts & celebration",
             "https://www.amazon.co.uk/Best-Sellers/zgbs/kids"),
    "party": ("Party supplies", "Gifts & celebration",
              "https://www.amazon.co.uk/Best-Sellers/zgbs/kitchen/3149053031"),
    "jewellery": ("Jewellery", "Gifts & celebration",
                  "https://www.amazon.co.uk/Best-Sellers/zgbs/jewelry"),
    "handmade": ("Handmade", "Gifts & celebration",
                 "https://www.amazon.co.uk/Best-Sellers/zgbs/handmade"),

    # ── Beauty & personal ──
    "beauty": ("Beauty", "Beauty & personal",
               "https://www.amazon.co.uk/Best-Sellers/zgbs/beauty"),
    "health": ("Health & personal care", "Beauty & personal",
               "https://www.amazon.co.uk/Best-Sellers/zgbs/drugstore"),
    "fashion": ("Fashion", "Beauty & personal",
                "https://www.amazon.co.uk/Best-Sellers/zgbs/fashion"),

    # ── Media & tech ──
    "books": ("Books", "Media & tech",
              "https://www.amazon.co.uk/Best-Sellers/zgbs/books"),
    "electronics": ("Electronics", "Media & tech",
                    "https://www.amazon.co.uk/Best-Sellers/zgbs/electronics"),
    "computers": ("Computers", "Media & tech",
                  "https://www.amazon.co.uk/Best-Sellers/zgbs/computers"),
    "videogames": ("Video games", "Media & tech",
                   "https://www.amazon.co.uk/Best-Sellers/zgbs/videogames"),

    # ── Everyday ──
    "grocery": ("Grocery", "Everyday",
                "https://www.amazon.co.uk/Best-Sellers/zgbs/grocery"),
    "pets": ("Pet supplies", "Everyday",
             "https://www.amazon.co.uk/Best-Sellers/zgbs/pet-supplies"),
    "sports": ("Sports & outdoors", "Everyday",
               "https://www.amazon.co.uk/Best-Sellers/zgbs/sports"),
    "baby": ("Baby", "Everyday",
             "https://www.amazon.co.uk/Best-Sellers/zgbs/baby"),
}

# Words that are never brand names — skipped when inferring brand from a title.
GENERIC_WORDS = {
    "the", "a", "an", "and", "for", "with", "new", "big", "best", "top",
    "birthday", "funny", "happy", "christmas", "personalised", "personalized",
    "cute", "luxury", "premium", "official", "original", "pack", "set", "pcs",
    "amazon", "gift", "gifts", "card", "cards", "mini", "large", "small",
    "black", "white", "red", "blue", "green", "pink", "gold", "silver",
    "1st", "2nd", "3rd", "4th", "5th", "10th", "16th", "18th", "21st",
    "25th", "30th", "40th", "50th", "60th", "70th", "80th", "90th", "100th",
    "2024", "2025", "2026", "professional", "ultra", "super", "smart",
}


def fetch(url: str, attempt: int = 1) -> str:
    """Fetch a URL through ScraperAPI with retry."""
    try:
        r = requests.get(
            "https://api.scraperapi.com",
            params={
                "api_key": API_KEY,
                "url": url,
                "country_code": "gb",
                "render": "true",
            },
            timeout=180,
        )
        r.raise_for_status()
        return r.text
    except Exception:
        if attempt < 2:
            time.sleep(5)
            return fetch(url, attempt + 1)
        raise


def clean_text(el) -> str:
    return " ".join(el.get_text().split()) if el else ""


def parse_products(html: str) -> list[dict]:
    """Extract products from an Amazon best-sellers page."""
    soup = BeautifulSoup(html, "html.parser")
    products, seen = [], set()

    cards = (soup.select("#gridItemRoot")
             or soup.select(".zg-grid-general-faceout")
             or soup.select("[id^='zg-ordered-list'] li"))

    for card in cards:
        link = card.select_one("a[href*='/dp/']")
        if not link:
            continue
        m = re.search(r"/dp/([A-Z0-9]{10})", link.get("href", ""))
        if not m:
            continue
        asin = m.group(1)
        if asin in seen:
            continue

        card_text = card.get_text(" ", strip=True)

        # Image — must be a genuine product image, not a nav sprite
        image = ""
        img = card.select_one("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src and "nav-sprite" not in src and "gno/sprites" not in src:
                if "/images/I/" in src or src.endswith((".jpg", ".png", ".jpeg")):
                    image = src
        if not image:
            continue

        # Rank
        rank = None
        rank_el = card.select_one(".zg-bdg-text, .zg-badge-text")
        if rank_el:
            rm = re.search(r"#?(\d+)", rank_el.get_text())
            if rm:
                rank = int(rm.group(1))

        # Title — longest candidate wins
        title = ""
        for sel in ("a[href*='/dp/']", "[class*='line-clamp']", "[class*='truncate']"):
            for el in card.select(sel):
                t = clean_text(el)
                if len(title) < len(t) < 400:
                    title = t

        # Rating and review count
        rating_val = None
        rating_el = card.select_one(".a-icon-alt")
        rating_txt = clean_text(rating_el) or card_text
        rm = re.search(r"(\d(?:\.\d)?)\s+out of\s+5", rating_txt)
        if rm:
            rating_val = float(rm.group(1))

        reviews_val = None
        # Review counts appear as a standalone number, often in .a-size-small
        for el in card.select(".a-size-small, [class*='review']"):
            t = clean_text(el).replace(",", "")
            if t.isdigit():
                reviews_val = int(t)
                break
        if reviews_val is None:
            rc = re.search(r"\(([\d,]+)\)", card_text)
            if rc:
                reviews_val = int(rc.group(1).replace(",", ""))

        # Price
        price_txt, price_val = "", None
        pm = re.search(r"([£$€])\s*([\d,]+(?:\.\d{1,2})?)", card_text)
        if pm:
            price_txt = f"{pm.group(1)}{pm.group(2)}"
            try:
                price_val = float(pm.group(2).replace(",", ""))
            except ValueError:
                pass

        # Badges
        badges = []
        low = card_text.lower()
        if "amazon's choice" in low or "amazons choice" in low:
            badges.append("Amazon's Choice")
        if "best seller" in low or "bestseller" in low:
            badges.append("Best Seller")
        if "limited time deal" in low:
            badges.append("Deal")
        if "small business" in low:
            badges.append("Small Business")

        products.append({
            "asin": asin,
            "rank": rank,
            "title": title,
            "brand": infer_brand(title),
            "price": price_txt,
            "price_value": price_val,
            "rating": rating_val,
            "reviews": reviews_val,
            "badges": badges,
            "image": image,
            "url": f"https://www.amazon.co.uk/dp/{asin}",
        })
        seen.add(asin)

    products.sort(key=lambda p: (p["rank"] is None, p["rank"] or 9999))
    for i, p in enumerate(products):
        if p["rank"] is None:
            p["rank"] = i + 1
    return products[:TOP_N]


def infer_brand(title: str) -> str:
    """Best-effort brand from a product title."""
    if not title:
        return ""
    # Explicit separators — Amazon sellers often write "BRAND | product name"
    for sep in ("|", " - ", ",", ":"):
        if sep in title:
            head = title.split(sep)[0].strip()
            words = head.split()
            if 1 <= len(words) <= 3 and words[0].lower() not in GENERIC_WORDS:
                return head
            break
    # Otherwise take leading words that look like a proper noun
    out = []
    for w in title.split():
        bare = re.sub(r"[^\w&'-]", "", w)
        if not bare or bare.lower() in GENERIC_WORDS:
            break
        # Brand-ish: capitalised or ALL CAPS
        if bare[0].isupper() or bare.isupper():
            out.append(bare)
        else:
            break
        if len(out) == 2:
            break
    return " ".join(out)


def brand_tally(products: list[dict]) -> list[dict]:
    counts = Counter()
    for p in products:
        b = (p.get("brand") or "").strip()
        if len(b) > 1:
            counts[b] += 1
    return [{"brand": b, "count": c} for b, c in counts.most_common(12)]


def category_stats(products: list[dict]) -> dict:
    ratings = [p["rating"] for p in products if p.get("rating")]
    reviews = [p["reviews"] for p in products if p.get("reviews")]
    prices = [p["price_value"] for p in products if p.get("price_value")]
    return {
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "total_reviews": sum(reviews) if reviews else None,
        "median_price": round(sorted(prices)[len(prices) // 2], 2) if prices else None,
        "with_price": len(prices),
    }


def load_previous_ranks() -> dict:
    """{slug: {asin: rank}} from the last successful run, for movement arrows."""
    if not LATEST.exists():
        return {}
    try:
        prev = json.loads(LATEST.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for slug, cat in (prev.get("categories") or {}).items():
        out[slug] = {p["asin"]: p["rank"] for p in (cat.get("products") or []) if p.get("asin")}
    return out


def scrape_one(slug: str, label: str, group: str, url: str) -> tuple[str, dict]:
    try:
        html = fetch(url)
        products = parse_products(html)
        if not products:
            raise RuntimeError("0 products parsed — likely a CAPTCHA or layout change")
        return slug, {
            "ok": True,
            "label": label,
            "group": group,
            "url": url,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "count": len(products),
            "products": products,
            "brands": brand_tally(products),
            "stats": category_stats(products),
        }
    except Exception as e:
        return slug, {"ok": False, "label": label, "group": group, "url": url,
                      "error": str(e),
                      "failed_at": datetime.now(timezone.utc).isoformat()}


def main():
    DATA_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    only = os.environ.get("ONLY_CATEGORIES", "").strip()
    targets = dict(CATEGORIES)
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        targets = {k: v for k, v in CATEGORIES.items() if k in wanted}
        print(f"Scraping subset: {sorted(targets)}")

    prev_ranks = load_previous_ranks()
    prev_full = {}
    if LATEST.exists():
        try:
            prev_full = json.loads(LATEST.read_text(encoding="utf-8")).get("categories", {}) or {}
        except Exception:
            pass

    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(scrape_one, s, l, g, u): s
                   for s, (l, g, u) in targets.items()}
        for fut in as_completed(futures):
            slug, res = fut.result()
            if res["ok"]:
                print(f"  ✓ {slug:18s} {res['count']:3d} products", flush=True)
            else:
                print(f"  ✗ {slug:18s} {res['error'][:60]}", flush=True)
            results[slug] = res

    # Attach rank movement, and fall back to previous data on failure
    for slug, cat in results.items():
        if cat["ok"]:
            old = prev_ranks.get(slug, {})
            for p in cat["products"]:
                prev = old.get(p["asin"])
                p["prev_rank"] = prev
                p["delta"] = (prev - p["rank"]) if prev is not None else None
                p["is_new"] = prev is None and bool(old)
        else:
            old_cat = prev_full.get(slug) or {}
            if old_cat.get("products"):
                cat["products"] = old_cat["products"]
                cat["brands"] = old_cat.get("brands", [])
                cat["stats"] = old_cat.get("stats", {})
                cat["count"] = old_cat.get("count", len(old_cat["products"]))
                cat["scraped_at"] = old_cat.get("scraped_at")
                cat["stale"] = True

    now = datetime.now(timezone.utc)
    payload = {
        "generated_at": now.isoformat(),
        "top_n": TOP_N,
        "groups": sorted({g for _, g, _ in
                          ((v[0], v[1], v[2]) for v in CATEGORIES.values())}),
        "categories": results,
    }

    LATEST.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    snap = HISTORY_DIR / f"{now.strftime('%Y-%m-%dT%H')}.json"
    snap.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # Keep only the 60 most recent snapshots so the repo stays small
    snaps = sorted(HISTORY_DIR.glob("*.json"))
    for old in snaps[:-60]:
        old.unlink()

    ok = sum(1 for c in results.values() if c["ok"])
    print(f"\nDone: {ok}/{len(results)} categories succeeded")
    print(f"Wrote {LATEST} and {snap}")


if __name__ == "__main__":
    main()
