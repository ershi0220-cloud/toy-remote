#!/usr/bin/env python3
"""Private, request-only Termux location bridge. No coordinates are written to public repositories."""
import base64
import datetime as dt
import fcntl
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = Path.home() / ".config" / "guz-location"
CFG = BASE / "config.json"
STATE = BASE / "last-seq.txt"
LOCK = BASE / "watcher.lock"
LOG = BASE / "agent.log"
BOOT = Path.home() / ".termux" / "boot" / "start-guz-location"
POLL_SECONDS = 25

def github(method, endpoint, token, payload=None, missing_ok=False):
    url = "https://api.github.com/" + endpoint.lstrip("/")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "PersonalTermuxLocationBridge/1.0",
        "Cache-Control": "no-cache"
    }
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and missing_ok:
            return None
        raise RuntimeError("GitHub API returned HTTP " + str(exc.code)) from exc

def get_file(repo, token, path):
    obj = github("GET", "repos/" + repo + "/contents/" + path, token, missing_ok=True)
    if obj is None:
        return None, None
    raw = base64.b64decode(obj["content"].replace("\n", ""))
    return json.loads(raw.decode("utf-8")), obj["sha"]

def put_file(repo, token, path, value):
    previous, sha = get_file(repo, token, path)
    encoded = base64.b64encode(json.dumps(value, ensure_ascii=False, indent=2).encode()).decode()
    payload = {"message": "Update location bridge " + path, "content": encoded}
    if sha:
        payload["sha"] = sha
    github("PUT", "repos/" + repo + "/contents/" + path, token, payload)

def config():
    if not CFG.exists():
        raise RuntimeError("No configuration. Run: python ~/location_bridge.py setup")
    return json.loads(CFG.read_text(encoding="utf-8"))

def locate():
    # Fresh network location generally succeeds indoors; do not use a stale cached GPS result.
    result = subprocess.run(
        ["termux-location", "-p", "network", "-r", "once"],
        capture_output=True, text=True, timeout=35, check=True
    )
    data = json.loads(result.stdout)
    lat, lon = data["latitude"], data["longitude"]
    params = urllib.parse.urlencode({
        "lat": lat, "lon": lon, "format": "jsonv2",
        "accept-language": "zh-CN", "addressdetails": 1, "zoom": 17
    })
    req = urllib.request.Request(
        "https://nominatim.openstreetmap.org/reverse?" + params,
        headers={"User-Agent": "PersonalTermuxLocationBridge/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        place = json.load(res)
    return {
        "place": place.get("display_name", "无法匹配中文地名"),
        "accuracy_m": data.get("accuracy"),
        "provider": data.get("provider", "network"),
        "note": "逆地理编码显示附近地点，不一定是实际门牌号"
    }

def setup():
    BASE.mkdir(parents=True, exist_ok=True)
    os.chmod(BASE, 0o700)
    print("仅用私有仓库收发定位。不要使用公开的 toy-remote 仓库。")
    repo = input("Private GitHub repository [ershi0220-cloud/location-bridge]: ").strip()
    repo = repo or "ershi0220-cloud/location-bridge"
    if repo.count("/") != 1:
        raise RuntimeError("Use the owner/repository format")
    token = getpass.getpass("Fine-grained token (input hidden): ").strip()
    if not token:
        raise RuntimeError("Missing token")
    meta = github("GET", "repos/" + repo, token)
    if not meta.get("private", False):
        raise RuntimeError("Refusing to store location in a PUBLIC repository")
    CFG.write_text(json.dumps({"repo": repo, "token": token}), encoding="utf-8")
    os.chmod(CFG, 0o600)
    for name, initial in (
        ("request.json", {"seq": 0, "action": "idle", "issued_at": None}),
        ("result.json", {"seq": 0, "status": "waiting"})
    ):
        existing, _ = get_file(repo, token, name)
        if existing is None:
            put_file(repo, token, name, initial)
    BOOT.parent.mkdir(parents=True, exist_ok=True)
    BOOT.write_text(
        "#!/data/data/com.termux/files/usr/bin/sh\n"
        "termux-wake-lock\n"
        "nohup python \"$HOME/location_bridge.py\" run "
        ">> \"$HOME/.config/guz-location/agent.log\" 2>&1 < /dev/null &\n",
        encoding="utf-8"
    )
    BOOT.chmod(0o700)
    print("Private repo verified; request/result files ready.")
    print("Boot script installed:", BOOT)
    start()
    print("Please open the Termux:Boot app once and allow background location/battery operation.")
    print("Never paste your token into ChatGPT or a public repo.")

def start():
    BASE.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["termux-wake-lock"], timeout=8, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    except Exception:
        pass
    with LOG.open("a", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    print("Watcher launch requested. Read log if needed:", LOG)

def fresh(issued):
    if not issued:
        return False
    try:
        timestamp = dt.datetime.fromisoformat(str(issued).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return False
        age = (dt.datetime.now(dt.timezone.utc) - timestamp).total_seconds()
        return -60 <= age <= 600
    except ValueError:
        return False

def run():
    BASE.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A location watcher is already running.", flush=True)
            return
        cfg = config()
        repo, token = cfg["repo"], cfg["token"]
        last_seq = int(STATE.read_text().strip()) if STATE.exists() else 0
        print("Private location watcher started.", flush=True)
        while True:
            try:
                req, _ = get_file(repo, token, "request.json")
                if isinstance(req, dict):
                    seq = int(req.get("seq", 0))
                    if seq > last_seq:
                        last_seq = seq
                        STATE.write_text(str(seq), encoding="utf-8")
                        if req.get("action") == "locate" and fresh(req.get("issued_at")):
                            try:
                                response = {"seq": seq, "status": "ok", **locate()}
                            except Exception as exc:
                                response = {"seq": seq, "status": "error",
                                            "message": str(exc)[:180]}
                            response["responded_at"] = dt.datetime.now(
                                dt.timezone.utc).isoformat()
                            put_file(repo, token, "result.json", response)
                            print("Processed location request", seq, flush=True)
                        elif req.get("action") == "locate":
                            put_file(repo, token, "result.json",
                                     {"seq": seq, "status": "expired",
                                      "message": "Request was not recent"})
                time.sleep(POLL_SECONDS)
            except Exception as exc:
                print("Temporary connection error:", str(exc)[:120], flush=True)
                time.sleep(40)

if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if task == "setup":
            setup()
        elif task == "run":
            run()
        elif task == "start":
            start()
        elif task == "status":
            print("Configured:", CFG.exists(), "Last request:", STATE.read_text()
                  if STATE.exists() else "none")
        else:
            print("Usage: python ~/location_bridge.py setup|start|status|run")
    except Exception as exc:
        print("ERROR:", str(exc))
        sys.exit(1)
