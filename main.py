#!/usr/bin/env python3
"""Fetch live BMTC bus tracking status for a set of routes on a fixed interval.

GPS observations are appended to data/gps.parquet. For any interval where a
route's live tracking returns no bus GPS locations, a screenshot of that
route's live-tracking page is saved under images/.
"""

import argparse
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://bmtcmobileapi.karnataka.gov.in/WebAPI/SearchByRouteDetails_v4"
SITE_HOME = "https://nammabmtcapp.karnataka.gov.in/"
SITE_TRACK = "https://nammabmtcapp.karnataka.gov.in/commuter/search-by-route"
SITE_TRACK_BUS = "https://nammabmtcapp.karnataka.gov.in/commuter/track-a-bus"

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "lan": "en",
    "deviceType": "WEB",
    "Origin": "https://nammabmtcapp.karnataka.gov.in",
    "Referer": "https://nammabmtcapp.karnataka.gov.in/",
}

DEFAULT_ROUTES = [
    ("MF-22E", 9008),
    ("MF-22EA", 9007),
    ("MF-22K", 9009),
    ("MF-22ET", 9417),
    ("MF-22G", 9375)
]
DEFAULT_INTERVAL_MIN = 10
STALE_AFTER_MIN = 15
IST = timezone(timedelta(hours=5, minutes=30))

DATA_FILE = Path("data/gps.parquet")
IMAGE_DIR = Path("images")
SE_BENGALURU = {"lat": 12.89, "lng": 77.71, "zoom": 12}
SIDEBAR_PAN_X = 408
MAP_OK_STDDEV = 15.0


def map_region_stddev(path):
    """Measure visible map detail; low variance indicates a blank map."""
    from PIL import Image

    region = Image.open(path).convert("L").crop((0, 120, 430, 880))
    region = region.resize((140, 150))
    px = list(region.getdata())
    n = len(px)
    mean = sum(px) / n
    return (sum((p - mean) ** 2 for p in px) / n) ** 0.5

# Capture Google Map instances before the site creates them.
MAP_CAPTURE_HOOK = r"""
(() => {
  function wrap(Orig){ if(!Orig||Orig.__hooked) return Orig;
    function H(...a){const i=new Orig(...a);(window.__maps=window.__maps||[]).push(i);return i;}
    H.prototype=Orig.prototype; Object.setPrototypeOf(H,Orig); H.__hooked=true; return H; }
  function trapMaps(m){ const d=Object.getOwnPropertyDescriptor(m,'Map'); let cur=d&&d.value;
    try{ Object.defineProperty(m,'Map',{configurable:true,
      get(){return cur;}, set(v){cur=wrap(v);}}); if(cur) cur=wrap(cur); }catch(e){} }
  let _g;
  Object.defineProperty(window,'google',{configurable:true,
    get(){return _g;},
    set(v){ _g=v; if(v){ let _m;
      try{ Object.defineProperty(v,'maps',{configurable:true,
        get(){return _m;}, set(mm){_m=mm; if(mm) trapMaps(mm);} }); }catch(e){} } }});
})();
"""

VEHICLE_FIELDS = [
    "vehicleid", "vehiclenumber", "servicetypeid", "servicetype",
    "centerlat", "centerlong", "heading", "eta",
    "currentstop", "nextstop", "laststop", "stopCoveredStatus",
    "lastlocationid", "currentlocationid", "nextlocationid",
    "tripposition", "lastrefreshon", "lastreceiveddatetimeflag",
    "sch_arrivaltime", "sch_departuretime",
    "actual_arrivaltime", "actual_departuretime",
]


def fetch_route(route_id, session):
    """Return the parsed API response for a route, or None on failure."""
    try:
        resp = session.post(
            API_URL,
            json={"routeid": route_id, "servicetypeid": 0},
            headers=API_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  ! request failed for route {route_id}: {exc}")
        return None


def lastrefresh_age_minutes(lastrefreshon, fetched_at):
    """Return GPS refresh age in minutes, or None for invalid input."""
    if not lastrefreshon:
        return None
    try:
        refreshed = datetime.strptime(
            lastrefreshon, "%d-%m-%Y %H:%M:%S"
        ).replace(tzinfo=IST)
    except (ValueError, TypeError):
        return None
    return (datetime.fromisoformat(fetched_at) - refreshed).total_seconds() / 60


def extract_records(payload, route_name, route_id, fetched_at):
    """Flatten route vehicles and report whether any has fresh GPS."""
    records = []
    route_no = None
    trackable = False
    for direction in ("up", "down"):
        block = payload.get(direction) or {}
        data = block.get("data") or []
        if data and route_no is None:
            route_no = data[0].get("routeno")
        for vehicle in block.get("mapData") or []:
            age = lastrefresh_age_minutes(
                vehicle.get("lastrefreshon"), fetched_at)
            fresh = age is not None and age <= STALE_AFTER_MIN
            trackable = trackable or fresh
            records.append({
                "fetched_at": fetched_at,
                "route_id": route_id,
                "route_name": route_name,
                "route_no": route_no,
                "direction": direction,
                "status": "OK" if fresh else "STALE",
                "lastrefresh_age_min": (
                    round(age, 1) if age is not None else None
                ),
                **{field: vehicle.get(field) for field in VEHICLE_FIELDS},
            })

    if not records:
        records = [{
            "fetched_at": fetched_at,
            "route_id": route_id,
            "route_name": route_name,
            "route_no": route_no,
            "direction": None,
            "status": "NO_GPS",
            "lastrefresh_age_min": None,
            **{field: None for field in VEHICLE_FIELDS},
        }]
    return records, trackable


def append_parquet(records):
    """Append records to the parquet file, creating it if needed."""
    if not records:
        return
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(records)
    if DATA_FILE.exists():
        frame = pd.concat([pd.read_parquet(DATA_FILE), frame], ignore_index=True)
    frame.to_parquet(DATA_FILE, index=False)


@lru_cache(maxsize=1)
def _r2_client():
    """Return the cached Cloudflare R2 client."""
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}"
        ".r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def upload_to_r2(local_path, key, content_type):
    """Upload a file to R2 without interrupting polling on failure."""
    try:
        _r2_client().upload_file(
            str(local_path), os.environ["R2_BUCKET"], key,
            ExtraArgs={"ContentType": content_type},
        )
        print(f"  ^ uploaded to R2: {key}")
    except Exception as exc:
        print(f"  ! R2 upload failed for {key}: {exc}")


def _r2_key(prefix, name):
    return f"{prefix.rstrip('/')}/{name}" if prefix else name


def upload_parquet():
    if not DATA_FILE.exists():
        return
    key = _r2_key(os.environ.get("R2_PREFIX", "data/"), DATA_FILE.name)
    upload_to_r2(DATA_FILE, key, "application/octet-stream")


def _stamp(fetched_at):
    return fetched_at.replace(":", "").replace("-", "").replace("T", "_")[:15]


def _unique_path(path):
    """Return a numbered path when the requested path already exists."""
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _goto(page, url):
    last = None
    for _ in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception as exc:
            last = exc
            page.wait_for_timeout(2000)
    raise last


def _wait_map_ready(page, timeout=25000):
    """Wait until the spinner is gone and map tiles are painted."""
    try:
        page.wait_for_function(
            """() => {
                const sp = document.querySelector('.ngx-spinner-overlay');
                const spinning = sp && sp.offsetParent !== null &&
                    getComputedStyle(sp).display !== 'none';
                const imgs = [...document.querySelectorAll('.gm-style img')];
                const loaded = imgs.filter(
                    i => i.complete && i.naturalWidth > 0).length;
                return !spinning && loaded > 12;
            }""",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


def _ensure_sidebar_open(page):
    """Open the route-details sidebar if needed."""
    for _ in range(4):
        heading = page.get_by_text("Route Details")
        if heading.count() and heading.first.is_visible():
            return True
        try:
            page.eval_on_selector("button.slider-toggler", "el => el.click()")
        except Exception:
            page.mouse.click(417, 213)
        page.wait_for_timeout(900)
    return False


def _frame_map(page, target, pan_x=0):
    """Frame the captured Google Map, optionally clear of the sidebar."""
    result = page.evaluate(
        """(a) => {
            const ms = window.__maps || [];
            if (!ms.length) return null;
            // Select the largest map; the sidebar also owns a hidden map.
            let m = ms[ms.length - 1], best = 0;
            for (const cand of ms) {
                try {
                    const r = cand.getDiv().getBoundingClientRect();
                    const area = r.width * r.height;
                    if (area > best) { best = area; m = cand; }
                } catch (e) {}
            }
            window.__mapIdle = false;
            m.setZoom(a.zoom);
            let center = {lat: a.lat, lng: a.lng};
            // Shift east in projected pixels to clear the sidebar.
            const proj = m.getProjection();
            if (a.panX && proj) {
                const scale = 2 ** m.getZoom();
                const p = proj.fromLatLngToPoint(
                    new google.maps.LatLng(a.lat, a.lng));
                const ll = proj.fromPointToLatLng(
                    new google.maps.Point(p.x + a.panX / scale, p.y));
                center = {lat: ll.lat(), lng: ll.lng()};
            }
            m.setCenter(center);
            google.maps.event.addListenerOnce(
                m, 'idle', () => { window.__mapIdle = true; });
            const c = m.getCenter();
            return {lat: c.lat(), lng: c.lng(), zoom: m.getZoom()};
        }""",
        {**target, "panX": pan_x},
    )
    if result is None:
        return False
    print(f"    map framed at ({result['lat']:.4f}, {result['lng']:.4f}) "
          f"z{result['zoom']}")
    try:
        page.wait_for_function(
            "() => window.__mapIdle === true", timeout=20000)
    except Exception:
        pass
    return True


def _hide_spinner(page):
    page.evaluate(
        "() => document.querySelectorAll('.ngx-spinner-overlay')"
        ".forEach(e => e.style.display = 'none')")


def _capture_with_retries(capture_once, out, label):
    """Retry a blank capture in up to four fresh browser sessions."""
    from playwright.sync_api import sync_playwright

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            for _ in range(4):
                browser = pw.chromium.launch()
                try:
                    capture_once(browser)
                finally:
                    browser.close()
                if map_region_stddev(out) >= MAP_OK_STDDEV:
                    break
        quality = map_region_stddev(out)
        note = "" if quality >= MAP_OK_STDDEV else "  (map may be blank)"
        print(f"  > screenshot saved: {out}{note}")
    except Exception as exc:  # screenshot is best-effort
        print(f"  ! screenshot failed for {label}: {exc}")
    if out.exists():
        upload_to_r2(out, _r2_key("images", out.name), "image/jpeg")


def _new_page(browser):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.add_init_script(MAP_CAPTURE_HOOK)
    return page


def _save_screenshot(page, out):
    _hide_spinner(page)
    page.screenshot(path=str(out), type="jpeg", quality=90, full_page=False)


def screenshot_route(route_name, fetched_at):
    """Capture a route map and its details in south-eastern Bengaluru."""
    out = _unique_path(IMAGE_DIR / f"{route_name}_{_stamp(fetched_at)}.jpg")

    def capture_once(browser):
        page = _new_page(browser)
        _goto(page, SITE_HOME)
        page.wait_for_timeout(1000)
        _goto(page, SITE_TRACK)
        page.wait_for_selector("input[type=text]", timeout=30000)
        page.wait_for_timeout(1500)

        box = page.query_selector("input[type=text]")
        box.click()
        box.type(route_name, delay=120)
        page.wait_for_timeout(2500)
        page.get_by_text(route_name, exact=True).first.click()
        page.wait_for_timeout(1500)
        page.get_by_role("button", name="search").first.click()
        page.get_by_text("Route Details").first.wait_for(timeout=20000)
        _wait_map_ready(page)

        if not _ensure_sidebar_open(page):
            print(f"  ! route-details sidebar not open for {route_name}")
        page.wait_for_timeout(1500)

        if not _frame_map(page, SE_BENGALURU, pan_x=SIDEBAR_PAN_X):
            print(f"  ! could not access map to frame {route_name}")
        else:
            _wait_map_ready(page)
            page.wait_for_timeout(800)
        _save_screenshot(page, out)

    _capture_with_retries(capture_once, out, route_name)


def screenshot_bus(route_name, busno, vehicleid, fetched_at):
    """Capture the last tracked position of a stale bus."""
    out = _unique_path(
        IMAGE_DIR / f"{route_name}_{busno}_{_stamp(fetched_at)}.jpg")
    url = f"{SITE_TRACK_BUS}?busno={busno}&vehicleid={vehicleid}"

    def capture_once(browser):
        page = _new_page(browser)
        _goto(page, SITE_HOME)
        page.wait_for_timeout(1000)
        _goto(page, url)
        try:
            page.get_by_text("Last Tracked").first.wait_for(timeout=20000)
        except Exception:
            pass
        _wait_map_ready(page)
        page.wait_for_timeout(1000)
        _save_screenshot(page, out)

    _capture_with_retries(capture_once, out, f"{route_name}/{busno}")


def run_once(routes, session, screenshots=True):
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{fetched_at}] polling {len(routes)} route(s)")
    all_records = []
    no_data_routes = []
    stale_buses = []

    for route_name, route_id in routes:
        payload = fetch_route(route_id, session)
        if payload is None:
            no_data_routes.append(route_name)
            continue
        records, trackable = extract_records(payload, route_name, route_id, fetched_at)
        all_records.extend(records)
        fresh, stale = (
            sum(row["status"] == status for row in records)
            for status in ("OK", "STALE")
        )
        detail = f"{fresh} live bus(es)"
        if stale:
            detail += f", {stale} stale"
        if not trackable:
            detail += " — UNTRACKABLE"
        print(f"  {route_name} ({route_id}): {detail}")
        if any(r["status"] == "NO_GPS" for r in records):
            no_data_routes.append(route_name)
        stale_buses.extend(
            (route_name, row["vehiclenumber"], row["vehicleid"])
            for row in records
            if row["status"] == "STALE"
        )

    append_parquet(all_records)
    if all_records:
        upload_parquet()

    if not screenshots:
        return
    for route_name in no_data_routes:
        screenshot_route(route_name, fetched_at)
    for route_name, busno, vehicleid in stale_buses:
        screenshot_bus(route_name, busno, vehicleid, fetched_at)


def parse_routes(spec):
    """Parse 'NAME:ID,NAME:ID' into a list of (name, id) tuples."""
    routes = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, route_id = part.partition(":")
        routes.append((name.strip(), int(route_id)))
    return routes


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--routes", type=parse_routes, default=DEFAULT_ROUTES,
        help="Comma-separated NAME:ID list "
             "(default: MF-22E:9008,MF-22EA:9007,MF-22K:9009,MF-22ET:9417,MF22G:9375)",
    )
    parser.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL_MIN,
        help="Minutes between polls (default: 10)",
    )
    parser.add_argument(
        "--once", action="store_true", help="Run a single poll and exit",
    )
    parser.add_argument(
        "--no-screenshots", action="store_true",
        help="Disable screenshots on GPS failure",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    session = requests.Session()
    screenshots = not args.no_screenshots

    if args.once:
        run_once(args.routes, session, screenshots)
        return

    print(f"Polling every {args.interval} min. Ctrl-C to stop.")
    while True:
        run_once(args.routes, session, screenshots)
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
