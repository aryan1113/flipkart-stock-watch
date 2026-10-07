#!/usr/bin/env python3
"""Check Flipkart size stock for links.json, write docs/status.json, push ntfy alerts.

Env: NTFY_TOPIC (optional) - ntfy.sh topic to notify. Stdlib only.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
STATUS = ROOT / "docs" / "status.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Size swatches: a tracker block with contentType InStock/OutOfStock:Variant, widgetType
# atlas_swatch_attribute and contentTitle = size ("Custom Amount" for some; the visible size is
# then the next label_0 text). Colour swatches use atlas_swatch_image.
# The page also holds click-event payloads (selectedOptionValue) that report InStock for all sizes: skip.
# Also "Scarcity:Variant" = in stock but few left; only OutOfStock means unavailable.
BLOCK = re.compile(r'"contentType":"(InStock|Scarcity|OutOfStock):Variant"')
LABEL = re.compile(r'"label_0":\{"value":\{"text":"([^"]+)"')
PRICE = re.compile(r'"finalPrice":\s*(\d+)')
MRP = re.compile(r'"mrp":\s*(\d+)')
OG_IMAGE = re.compile(r'property="og:image" content="([^"]+)"')
REVIEW_IMG = re.compile(r'imageId=(blobio-imr_[\w]+\.jpg)')
TITLE = re.compile(r"<title>(.*?)</title>", re.S)


HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


def fetch(url, tries=4):
    # Flipkart answers 529/429 when it throttles; back off and retry.
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.geturl(), r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503, 529) or n == tries - 1:
                raise
            time.sleep(10 * (n + 1))


def parse(html):
    sizes = {}
    starts = [m for m in BLOCK.finditer(html)]
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(html)
        seg = html[m.start():min(end, m.start() + 4000)]
        if '"widgetType":"atlas_swatch_attribute"' not in seg or "selectedOptionValue" in seg[:200]:
            continue
        title = re.search(r'"contentTitle":"([^"]*)"', seg).group(1)
        if title == "Custom Amount":
            label = LABEL.search(html, m.start())
            title = label.group(1) if label else title
        sizes[title] = m.group(1) != "OutOfStock"
    t = TITLE.search(html)
    title = t.group(1).split(" - Buy")[0].strip() if t else ""
    return title, sizes


def review_photos(product_url, limit=6):
    """Buyer-uploaded photos from the reviews page (real-life shots, not studio)."""
    url = product_url.replace("/p/", "/product-reviews/", 1)
    _, html = fetch(url)
    ids = list(dict.fromkeys(REVIEW_IMG.findall(html)))[:limit]
    return [f"https://rukminim2.flixcart.com/blobio/376/376/imr/{i}?q=80" for i in ids]


def notify(topic, title, msg, click=None, priority="high"):
    if not topic:
        return
    headers = {"Title": title, "Priority": priority, "Tags": "athletic_shoe"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=msg.encode(), headers=headers)
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print("ntfy failed:", e, file=sys.stderr)


def main():
    cfg = json.loads((ROOT / "links.json").read_text())
    want = cfg["size"]
    topic = os.environ.get("NTFY_TOPIC")
    prev = {}
    if STATUS.exists():
        prev = {i["short_url"]: i for i in json.loads(STATUS.read_text()).get("items", [])}
    items = []
    for it in cfg["items"]:
        old = prev.get(it["url"], {})
        rec = {"name": it["name"], "budget": it.get("budget"), "offers": it.get("offers", {}), "short_url": it["url"],
               "url": it["url"], "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            final, html = fetch(it.get("product_url", it["url"]))
            title, sizes = parse(html)
            rec["url"] = it.get("product_url", it["url"])
            rec["title"] = title
            if not sizes:
                raise RuntimeError("no size info found (blocked or layout changed)")
            rec["sizes"] = sizes
            if want not in sizes:
                raise RuntimeError(f"size {want} not listed on page")
            rec["in_stock"] = sizes[want]
            rec["error"] = None
            p, mrp, img = PRICE.search(html), MRP.search(html), OG_IMAGE.search(html)
            rec["price"] = int(p.group(1)) if p else old.get("price")
            rec["mrp"] = int(mrp.group(1)) if mrp else old.get("mrp")
            rec["image"] = img.group(1) if img else old.get("image")
            if old.get("price") and rec["price"] and rec["price"] < old["price"]:
                notify(topic, "Price drop", f"{it['name']}: Rs {old['price']} -> Rs {rec['price']}",
                       click=rec["url"], priority="high")
            rec["photos"] = old.get("photos", [])
            if not rec["photos"] or time.time() - old.get("photos_ts", 0) > 12 * 3600:
                try:
                    rec["photos"] = review_photos(rec["url"]) or rec["photos"]
                    rec["photos_ts"] = time.time()
                except Exception as e:
                    print("photos failed:", e, file=sys.stderr)
            rec["photos_ts"] = rec.get("photos_ts", old.get("photos_ts", 0))
            if rec["in_stock"] and not old.get("in_stock"):
                notify(topic, f"Size {want} IN STOCK", f"{it['name']} - size {want} is available now",
                       click=rec["url"], priority="urgent")
        except Exception as e:
            rec.update(error=str(e), sizes=old.get("sizes", {}), price=old.get("price"), mrp=old.get("mrp"),
                       image=old.get("image"), photos=old.get("photos", []), photos_ts=old.get("photos_ts", 0), in_stock=old.get("in_stock", False), stale=True)
            if not old.get("error"):
                notify(topic, "Flipkart watcher problem", f"{it['name']}: {e}", priority="default")
        items.append(rec)
        time.sleep(3)
        print(f"{it['name']}: in_stock={rec['in_stock']} err={rec['error']}")
    STATUS.write_text(json.dumps({"size": want, "offers_as_of": cfg.get("offers_as_of"), "card_pct": cfg.get("card_pct", {}), "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "items": items}, indent=1))


if __name__ == "__main__":
    main()
