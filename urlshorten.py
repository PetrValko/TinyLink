import os
import json
import re
import string
import threading
import secrets
from flask import Flask, request, render_template, redirect, abort, send_from_directory
import urllib.parse
import requests

# Config
HOST = "127.0.0.1"
PORT = 5000

# Initialize Flask with explicit folders
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "urls.json")
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)

lock = threading.Lock()

# Base62 alphabet for random slugs
ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase

# URL must start with http(s)
URL_RE = re.compile(r"^(https?://)[^\s/$.?#].[^\s]*$", re.IGNORECASE)

def add_default_scheme(u: str) -> str:
    # If scheme is missing, assume https
    parsed = urllib.parse.urlparse(u)
    if not parsed.scheme:
        return "https://" + u
    return u

def canonicalize(u: str) -> str:
    # Lowercase host, strip a single trailing slash on path, keep query/fragment
    parsed = urllib.parse.urlparse(u)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]
    # Remove trailing slash on bare "/" path
    if path == "/":
        path = ""
    # Rebuild
    return urllib.parse.urlunparse((scheme, netloc, path, "", parsed.query, parsed.fragment))

def probe_scheme(url_no_scheme: str, timeout=2.0) -> str:
    # Try https first, then http; return the first that responds to HEAD/GET
    for scheme in ("https://", "http://"):
        test = scheme + url_no_scheme
        try:
            # HEAD first; if method not allowed, fall back to GET with stream to avoid downloading
            r = requests.head(test, timeout=timeout, allow_redirects=True)
            if r.status_code < 400:
                return test
        except Exception:
            pass
        try:
            r = requests.get(test, timeout=timeout, allow_redirects=True, stream=True)
            if r.status_code < 400:
                return test
        except Exception:
            pass
    # Default to https if all else fails
    return "https://" + url_no_scheme

def normalize_input(u: str) -> str:
    u = u.strip()
    if not u:
        return ""
    parsed = urllib.parse.urlparse(u)
    if not parsed.scheme:
        # handle inputs like "www.youtube.com" or "youtube.com"
        url_no_scheme = u
        # if user prefixed with //example.com
        if u.startswith("//"):
            url_no_scheme = u[2:]
        u = probe_scheme(url_no_scheme)
    # Canonicalize now that a scheme exists
    return canonicalize(u)

def stable_bundle_key(urls: list[str]) -> tuple[str, ...]:
    # Unique + sorted canonical URLs so order doesn’t matter
    uniq = sorted(set(urls))
    return tuple(uniq)

def random_base62(n: int = 8) -> str:
    # Cryptographically secure random slug generation
    return "".join(secrets.choice(ALPHABET) for _ in range(n))

def load_store():
    if not os.path.exists(DATA_FILE):
        return {"map": {}}  # code -> str (single) or list[str] (bundle)
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"map": {}}

def save_store(data: dict):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, DATA_FILE)

@app.route("/", methods=["GET"])
def root_to_shorten():
    # Convenience: redirect home to the form
    return redirect("/shorten/", code=302)

# GET: TinyURL-like form under /shorten/
@app.route("/shorten/", methods=["GET"])
def shorten_home():
    return render_template("index.html")

# POST: create short link(s)
# - Single URL: create or reuse a direct code; render single-result list
# - Multiple URLs (newline-separated): create a single bundle code mapping to list; render bundle link
@app.route("/shorten/", methods=["POST"])
def shorten_create():
    raw = (request.form.get("url") or "").strip()
    if not raw:
        return render_template("index.html", error="Please enter at least one URL.", url_value=""), 400

    # Split into lines; treat as bundle if more than one valid URL after cleaning
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    is_bulk = len(lines) > 1
    inputs = lines if is_bulk else [raw]

    raw = (request.form.get("url") or "").strip()
    lines = [ln for ln in (raw.splitlines() if "\n" in raw else [raw])]

    # Normalize lines
    normalized = []
    errors = []
    for ln in lines:
        n = normalize_input(ln)
        if not n:
            continue
        # Basic scheme check after normalization
        if not URL_RE.match(n):
            errors.append({"long_url": ln, "error": "Invalid URL"})
            continue
        normalized.append(n)

    # De-duplicate exact duplicates post-normalization
    normalized = list(dict.fromkeys(normalized))

    if not normalized:
        return render_template("index.html", error="No valid URLs provided.", url_value=raw), 400

    with lock:
        store = load_store()

        if len(normalized) > 1:
            # Bundle: compute stable key so [A,B] equals [B,A]
            key = stable_bundle_key(normalized)

            # Look for existing bundle by matching same list (stringify as storage-friendly)
            # Store bundles as {"_bundle": [urls]} to distinguish from single strings while staying JSON-compatible
            # Build an index scan (small local store ok)
            existing_code = None
            for c, v in store["map"].items():
                if isinstance(v, dict) and "_bundle" in v and tuple(v["_bundle"]) == key:
                    existing_code = c
                    break

            if existing_code:
                code = existing_code
            else:
                # Generate unique code
                code = None
                for _ in range(10):
                    cand = random_base62(8)
                    if cand not in store["map"]:
                        code = cand
                        break
                if not code:
                    return render_template("index.html", error="Could not generate unique code; please retry.",
                                           url_value=raw), 500
                # Persist bundle in canonical order
                store["map"][code] = {"_bundle": list(key)}
                save_store(store)

            bundle_url = request.host_url.rstrip("/") + "/shorten/" + code
            return render_template("bundle_result.html", bundle_url=bundle_url, urls=list(key), errors=errors)

        # Single URL: idempotent reuse if exists
        u = normalized[0]
        existing = next((c for c, dest in store["map"].items() if isinstance(dest, str) and dest == u), None)
        if existing:
            short_url = request.host_url.rstrip("/") + "/shorten/" + existing
            return render_template("result.html", results=[{"short_url": short_url, "long_url": u}], errors=errors)

        # Create new single code
        code = None
        for _ in range(10):
            cand = random_base62(8)
            if cand not in store["map"]:
                code = cand
                break
        if not code:
            return render_template("index.html", error="Could not generate unique code; please retry.",
                                   url_value=raw), 500

        store["map"][code] = u
        save_store(store)

        short_url = request.host_url.rstrip("/") + "/shorten/" + code
        return render_template("result.html", results=[{"short_url": short_url, "long_url": u}], errors=errors)


# Resolve: GET /shorten/<code>
# - If map[code] is a string: 302 redirect to that URL
# - If map[code] is a list: render a bundle open page listing all and auto-opening after 2s
@app.route("/shorten/<code>", methods=["GET"])
def resolve(code):
    # Accept only Base62 codes
    ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase
    if not code or any(c not in ALPHABET for c in code):
        abort(404)

    store = load_store()

    # Decide how to handle this code
    value = store.get("map", {}).get(code)
    if value is None:
        abort(404)
    if isinstance(value, dict) and "_bundle" in value and isinstance(value["_bundle"], list):
        return render_template("bundle_open.html", urls=value["_bundle"], code=code)
    if isinstance(value, list):
        # Backward compatibility if older bundles were plain lists
        return render_template("bundle_open.html", urls=value, code=code)
    if isinstance(value, str):
        return redirect(value, code=302)
    abort(404)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=True)


