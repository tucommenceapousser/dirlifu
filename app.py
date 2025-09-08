"""
Subdomains -> Files/Endpoints Enumerator (Flask)
Dev: trhacknon

Features:
- Accepts list of subdomains (paste or upload)
- For each subdomain runs in parallel:
    - waybackurls (if installed) to get historical endpoints
    - ffuf (if installed) to fuzz common paths (JSON output)
    - gospider (if installed) to crawl dynamic links
- Deduplicates & normalizes URLs
- Verifies URLs asynchronously with httpx (status, size, title)
- Streams progress via SSE for real-time frontend updates
- Stores per-task JSON results in ./results/<task_id>.json
- UI: interactive dashboard (index.html + main.js)
"""
import os
import subprocess
import json
import shutil
import time
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

from flask import Flask, render_template, request, jsonify, Response, send_file
from dotenv import load_dotenv
import httpx
from bs4 import BeautifulSoup

load_dotenv()

UPLOADS = Path("uploads")
RESULTS = Path("results")
WORDLISTS = Path("wordlists")
UPLOADS.mkdir(exist_ok=True)
RESULTS.mkdir(exist_ok=True)
WORDLISTS.mkdir(exist_ok=True)

# default small wordlist (for demo) if none provided
SMALL_WORDLIST = WORDLISTS / "small-common.txt"
if not SMALL_WORDLIST.exists():
    SMALL_WORDLIST.write_text("\n".join([
        "admin", "login", "dashboard", "api", "config", "index.php", "index.html",
        "robots.txt", "sitemap.xml", "uploads", "images", "assets", "css", "js"
    ]))

# Config
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
FFUF_THREADS = int(os.getenv("FFUF_THREADS", "40"))
FFUF_WORDLIST = os.getenv("FFUF_WORDLIST", str(SMALL_WORDLIST))
HTTPX_CONCURRENCY = int(os.getenv("HTTPX_CONCURRENCY", "40"))
VERIFY_SSL = os.getenv("VERIFY_SSL", "true").lower() in ("1","true","yes")

app = Flask(__name__, template_folder="templates", static_folder="static")
# in-memory task store (for demo). In prod, use persistent DB
TASKS = {}  # task_id -> {status, progress, logs[], result_path}

# Helpers
def has_binary(bin_name):
    return shutil.which(bin_name) is not None

def run_cmd_capture(cmd, timeout=120):
    """Run command, capture stdout. Return (returncode, stdout, stderr)"""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, text=True)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"

def normalize_url(base, url):
    """Try to normalize relative or absolute urls into absolute https/http urls."""
    if not url:
        return None
    url = url.strip()
    # if it's already absolute
    if url.startswith("http://") or url.startswith("https://"):
        return url
    # if url begins with // -> add scheme
    if url.startswith("//"):
        return "https:" + url
    # relative path
    if url.startswith("/"):
        parsed = urlparse(base)
        scheme = parsed.scheme or "https"
        return f"{scheme}://{parsed.netloc}{url}"
    # otherwise join
    return urljoin(base, url)

async def fetch_meta(client: httpx.AsyncClient, url: str):
    """Fetch URL and return metadata (status, size, title)"""
    try:
        r = await client.get(url, follow_redirects=True, timeout=20)
        ct = r.headers.get("content-type","")
        size = len(r.content or b"")
        title = ""
        if "text/html" in ct and size > 0:
            try:
                soup = BeautifulSoup(r.text, "html.parser")
                if soup.title and soup.title.string:
                    title = soup.title.string.strip()
            except Exception:
                title = ""
        return {"url": url, "status_code": r.status_code, "content_type": ct, "size": size, "title": title}
    except Exception as e:
        return {"url": url, "error": str(e)}

async def verify_urls_async(urls):
    """Verify many urls concurrently using httpx AsyncClient"""
    limits = httpx.Limits(max_connections=HTTPX_CONCURRENCY, max_keepalive_connections=HTTPX_CONCURRENCY)
    async with httpx.AsyncClient(verify=VERIFY_SSL, limits=limits, timeout=30) as client:
        tasks = [fetch_meta(client, u) for u in urls]
        results = []
        # run in batches to avoid creating thousands at once
        BATCH = 200
        for i in range(0, len(tasks), BATCH):
            batch = tasks[i:i+BATCH]
            res = await httpx.gather(*batch, return_exceptions=True) if hasattr(httpx, "gather") else await __gather_emulation(batch)
            # httpx.gather may not exist depending on version; fall back
            # But to be robust, run sequentially:
            if isinstance(res, Exception) or res is None:
                # fallback sequential
                for t in batch:
                    r = await t
                    results.append(r)
            else:
                results.extend(res)
        return results

# fallback gather emulation if needed
async def __gather_emulation(tasks):
    out = []
    for t in tasks:
        try:
            r = await t
            out.append(r)
        except Exception as e:
            out.append({"error": str(e)})
    return out

# Core per-subdomain worker
def process_subdomain(subdomain, task_id):
    """
    Steps per subdomain:
      - waybackurls (echo subdomain | waybackurls)
      - ffuf (http and https) with provided wordlist (if ffuf installed)
      - gospider (if installed)
      - collect urls, dedup, return list
    """
    task = TASKS[task_id]
    log = task["logs"]
    log.append(f"[{subdomain}] start processing")
    found = set()

    base_http = f"http://{subdomain}"
    base_https = f"https://{subdomain}"

    # 1) waybackurls
    if has_binary("waybackurls"):
        log.append(f"[{subdomain}] running waybackurls...")
        rc, out, err = run_cmd_capture(["bash","-lc", f"echo {subdomain} | waybackurls"], timeout=120)
        if rc == 0 and out:
            for line in out.splitlines():
                line=line.strip()
                if line:
                    found.add(line)
        else:
            log.append(f"[{subdomain}] waybackurls error: {err[:200]}")
    else:
        log.append(f"[{subdomain}] waybackurls not installed, skipping")

    # 2) gospider
    if has_binary("gospider"):
        log.append(f"[{subdomain}] running gospider (short crawl)...")
        # gospider can write to output folder, but we capture stdout
        cmd = ["gospider", "-s", base_https, "-d", "2", "--only-headers=false", "--timeout", "10"]
        rc, out, err = run_cmd_capture(cmd, timeout=60)
        if rc == 0 and out:
            # extract urls from stdout (simple heuristic)
            for line in out.splitlines():
                if line.strip().startswith("http"):
                    found.add(line.strip())
        else:
            log.append(f"[{subdomain}] gospider error/timeout or empty (rc={rc})")
    else:
        log.append(f"[{subdomain}] gospider not installed, skipping")

    # 3) ffuf (fuzz common paths) -> run both http and https
    if has_binary("ffuf"):
        log.append(f"[{subdomain}] running ffuf (fuzz) using {FFUF_WORDLIST}...")
        for scheme in ("http","https"):
            target = f"{scheme}://{subdomain}/FUZZ"
            out_json = UPLOADS / f"ffuf_{subdomain}_{scheme}.json"
            cmd = [
                "ffuf",
                "-u", target,
                "-w", FFUF_WORDLIST,
                "-t", str(FFUF_THREADS),
                "-mc", "200,301,302,403,401",
                "-of", "json",
                "-o", str(out_json)
            ]
            rc, o, e = run_cmd_capture(cmd, timeout=180)
            if rc == 0 and out_json.exists():
                try:
                    j = json.loads(out_json.read_text())
                    # ffuf json schema: "results" list with "url"
                    for r in j.get("results", []):
                        u = r.get("url")
                        if u:
                            found.add(u)
                except Exception as ex:
                    log.append(f"[{subdomain}] ffuf parse error: {ex}")
            else:
                log.append(f"[{subdomain}] ffuf {scheme} error or no output (rc={rc})")
    else:
        log.append(f"[{subdomain}] ffuf not installed, skipping")

    # 4) Basic probe: try common files directly (quick sweep)
    quick_common = ["robots.txt","sitemap.xml",".env","admin","login","index.php","index.html"]
    for p in quick_common:
        found.add(f"https://{subdomain}/{p}")

    # normalize URLs
    normalized = set()
    for u in list(found):
        n = normalize_url(f"https://{subdomain}", u)
        if n:
            normalized.add(n)

    log.append(f"[{subdomain}] collected {len(normalized)} candidate urls")
    return list(sorted(normalized))

# Task runner (runs in background thread via executor)
def run_task(task_id, subdomains):
    TASKS[task_id]["status"] = "running"
    TASKS[task_id]["progress"] = 0
    TASKS[task_id]["logs"].append(f"Task {task_id} started with {len(subdomains)} subdomains")
    results_by_sub = {}
    start = time.time()

    # per-subdomain parallel processing using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_to_sub = {ex.submit(process_subdomain, sd, task_id): sd for sd in subdomains}
        for i, fut in enumerate(as_completed(future_to_sub)):
            sd = future_to_sub[fut]
            try:
                urls = fut.result()
            except Exception as e:
                TASKS[task_id]["logs"].append(f"[{sd}] worker error: {e}")
                urls = []
            results_by_sub[sd] = urls
            # update progress
            TASKS[task_id]["progress"] = int((i+1)/len(subdomains)*80)  # 0-80% for collection
            TASKS[task_id]["logs"].append(f"[{sd}] done, {len(urls)} urls")
    TASKS[task_id]["logs"].append("Collection phase complete. Starting verification (httpx)...")

    # Merge and deduplicate all URLs
    all_urls = set()
    for sd, lst in results_by_sub.items():
        for u in lst:
            all_urls.add(u)
    all_urls = sorted(all_urls)
    TASKS[task_id]["logs"].append(f"Total unique urls before verification: {len(all_urls)}")

    # Verify URLs asynchronously using httpx
    # create event loop and run verification
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    async def verify_and_store():
        # limit concurrency
        client = httpx.AsyncClient(verify=VERIFY_SSL, timeout=30, limits=httpx.Limits(max_connections=HTTPX_CONCURRENCY))
        sem = asyncio.Semaphore(HTTPX_CONCURRENCY)
        results = []

        async def bounded_fetch(u):
            async with sem:
                try:
                    r = await client.get(u, follow_redirects=True)
                    size = len(r.content or b"")
                    title = ""
                    ct = r.headers.get("content-type","")
                    if "html" in ct.lower() and size>0:
                        try:
                            soup = BeautifulSoup(r.text, "html.parser")
                            if soup.title and soup.title.string:
                                title = soup.title.string.strip()
                        except Exception:
                            title = ""
                    return {"url": u, "status": r.status_code, "content_type": ct, "size": size, "title": title}
                except Exception as e:
                    return {"url": u, "error": str(e)}
        tasks = [bounded_fetch(u) for u in all_urls]
        # run in chunks
        B = 200
        for i in range(0, len(tasks), B):
            chunk = tasks[i:i+B]
            res = await asyncio.gather(*chunk, return_exceptions=True)
            for r in res:
                results.append(r)
        await client.aclose()
        return results

    try:
        verified = loop.run_until_complete(verify_and_store())
    except Exception as e:
        TASKS[task_id]["logs"].append(f"Verification error: {e}")
        verified = []
    finally:
        loop.close()

    # store final structured results
    out = {
        "task_id": task_id,
        "started_at": start,
        "finished_at": time.time(),
        "subdomains": results_by_sub,
        "all_urls_count": len(all_urls),
        "verified": verified
    }
    out_path = RESULTS / f"{task_id}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    TASKS[task_id]["result_path"] = str(out_path)
    TASKS[task_id]["status"] = "finished"
    TASKS[task_id]["progress"] = 100
    TASKS[task_id]["logs"].append(f"Task {task_id} finished. Results saved to {out_path}")
    return

# Flask routes
@app.route("/")
def index():
    return render_template("index.html", dev="trhacknon")

@app.route("/start_scan", methods=["POST"])
def start_scan():
    """
    JSON: { "subdomains": ["a.example.com","b.example.com"], "max_workers": int, "use_examples": bool }
    """
    data = request.get_json(force=True)
    subs = data.get("subdomains") or []
    if isinstance(subs, str):
        # allow newline separated paste
        subs = [s.strip() for s in subs.splitlines() if s.strip()]
    subs = list(dict.fromkeys([s.strip() for s in subs if s.strip()]))
    if not subs:
        return jsonify({"error":"no_subdomains"}), 400
    task_id = uuid.uuid4().hex[:12]
    TASKS[task_id] = {"status":"queued","progress":0,"logs":[f"Queued task {task_id}"], "result_path": None}
    # allow override workers
    global MAX_WORKERS
    mw = data.get("max_workers")
    if isinstance(mw, int) and mw>0:
        TASKS[task_id]["logs"].append(f"Setting max_workers={mw}")
        MAX_WORKERS = mw

    # launch background thread
    from threading import Thread
    t = Thread(target=run_task, args=(task_id, subs), daemon=True)
    t.start()
    return jsonify({"task_id": task_id})

@app.route("/task_status/<task_id>")
def task_status(task_id):
    t = TASKS.get(task_id)
    if not t:
        return jsonify({"error":"task_not_found"}), 404
    return jsonify({"status": t["status"], "progress": t.get("progress",0), "logs": t.get("logs", [])[:30], "result_path": t.get("result_path")})

@app.route("/stream/<task_id>")
def stream(task_id):
    """SSE stream for live logs"""
    def event_stream():
        last_index = 0
        while True:
            if task_id not in TASKS:
                yield f"data: {json.dumps({'error':'task_not_found'})}\n\n"
                break
            logs = TASKS[task_id]["logs"]
            if last_index < len(logs):
                for entry in logs[last_index:]:
                    payload = {"log": entry, "progress": TASKS[task_id].get("progress",0), "status": TASKS[task_id].get("status")}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last_index = len(logs)
            if TASKS[task_id]["status"] in ("finished","error"):
                break
            time.sleep(0.7)
    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/download/<task_id>")
def download(task_id):
    t = TASKS.get(task_id)
    if not t or not t.get("result_path"):
        return jsonify({"error":"not_ready"}), 404
    return send_file(t["result_path"], as_attachment=True, download_name=f"{task_id}.json")

if __name__ == "__main__":
    # run dev server
    app.run(host="0.0.0.0", port=5000, debug=True)
