#!/usr/bin/env python3
"""Fetch YOUR logged-in Flipkart price (the "fsp" in the page's ppd block) for every item in links.json,
write docs/my_prices.json and push it, so the Pages tracker shows what you would actually pay.

Runs on your Mac in a terminal and loops until Ctrl-C. It owns a separate Chrome profile (Playwright)
so it never touches your everyday browser.

  pip install playwright && playwright install chromium     # once
  python3 my_prices.py --login                              # once: log in with the OTP, then close the window
  python3 my_prices.py                                      # loop forever (every 30 min)

Flags: --once (single pass), --no-push (don't git commit/push), --headed (show the browser),
       --every MINUTES. Env: NTFY_TOPIC (optional) - alert when the login expires.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "docs" / "my_prices.json"
PROFILE = Path.home() / ".flipkart-watch-profile"
PPD = re.compile(r'"ppd":\{([^}]*)\}')
NUM = lambda key, s: (m := re.search(rf'"{key}":\s*(\d+)', s)) and int(m.group(1))


def launch(pw, headed):
    return pw.chromium.launch_persistent_context(
        str(PROFILE), headless=not headed, viewport={"width": 1280, "height": 900},
        locale="en-IN", args=["--disable-blink-features=AutomationControlled"])


def login(args):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = launch(pw, True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.flipkart.com/account/login")
        print("Log in to Flipkart in the opened window (OTP), then close the window.")
        try:
            page.wait_for_event("close", timeout=15 * 60 * 1000)
        except Exception:
            pass
        ctx.close()


def logged_in(ctx):
    """The account endpoint answers 200 with a profile when logged in, 401/redirect otherwise."""
    r = ctx.request.get("https://www.flipkart.com/account/?rd=0&link=home_account", max_redirects=0)
    return r.status == 200


def notify(msg):
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://ntfy.sh/{topic}", data=msg.encode(),
                headers={"Title": "Flipkart price script", "Priority": "high"}), timeout=20)
        except Exception as e:
            print("ntfy failed:", e, file=sys.stderr)


def one_pass(ctx, items):
    out = {}
    for it in items:
        rec = {"fsp": None, "nep": None, "error": None, "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            # Raw server HTML with the profile's login cookies; the rendered DOM is less reliable.
            r = ctx.request.get(it.get("product_url", it["url"]), timeout=60000)
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
            m = PPD.search(r.text())
            if not m:
                raise RuntimeError("no ppd price block (blocked or layout changed)")
            rec["fsp"], rec["nep"] = NUM("fsp", m.group(1)), NUM("nepPrice", m.group(1))
            rec["list"] = NUM("finalPrice", m.group(1))
            if not rec["fsp"]:
                raise RuntimeError("fsp missing")
        except Exception as e:
            rec["error"] = str(e)
        out[it["name"]] = rec
        print(f"{it['name']}: fsp={rec['fsp']} nep={rec['nep']} err={rec['error']}")
        time.sleep(4)
    return out


def push():
    def git(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True)
    git("add", "docs/my_prices.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", f"my prices {time.strftime('%H:%M')}")
    for _ in range(3):
        git("pull", "--rebase", "-q")
        r = git("push", "-q")
        if r.returncode == 0:
            return
        time.sleep(5)
    print("push failed:", r.stderr, file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--every", type=int, default=30)
    args = ap.parse_args()
    if args.login:
        return login(args)
    from playwright.sync_api import sync_playwright
    warned = False
    while True:
        cfg = json.loads((ROOT / "links.json").read_text())
        with sync_playwright() as pw:
            ctx = launch(pw, args.headed)
            try:
                ok = logged_in(ctx)
                items = one_pass(ctx, cfg["items"]) if ok else {}
            finally:
                ctx.close()
        if not ok:
            print("NOT LOGGED IN - run: python3 my_prices.py --login", file=sys.stderr)
            if not warned:
                notify("Flipkart login expired: run `python3 my_prices.py --login` on your Mac")
                warned = True
        else:
            warned = False
        OUT.write_text(json.dumps({"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                   "logged_in": ok, "items": items}, indent=1))
        if not args.no_push:
            push()
        if args.once:
            return
        time.sleep(args.every * 60)


if __name__ == "__main__":
    main()
