#!/usr/bin/env python3
"""List files/folders in Transmission's data folders that Transmission doesn't know about.

Nothing is ever deleted: orphans are only printed (and, with --rm-list, as an rm block
you can review).

CONFIG
  Settings come from a TOML file, and any command-line option overrides it.
  Config lookup order (first found wins):
    --config FILE, ./transmission-check.toml, ~/.config/transmission-check/config.toml
  See config.example.toml for every key.

PATH MAPPING (container vs host)
  Transmission reports paths as seen inside its container (/downloads/foo). This script
  runs on the host, where the same folder is e.g. /mnt/torrent/downloads/foo. A mapping
  CONTAINER=HOST tells it how to translate; the mapped host folders are also the roots
  that get scanned. Three ways to provide mappings (highest priority first):
    1. --map /downloads=/mnt/torrent/downloads   (repeatable)
    2. [paths] table in the config file
    3. --docker-container transmission : read the bind mounts from `docker inspect`
       (always correct, nothing to keep in sync with docker-compose)
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else str(text)


RED = lambda t: c(t, "1;31")
GREEN = lambda t: c(t, "32")
YELLOW = lambda t: c(t, "33")
BLUE = lambda t: c(t, "1;34")
DIM = lambda t: c(t, "2")
BOLD = lambda t: c(t, "1")


def die(msg):
    sys.exit(RED(f"error: {msg}"))


_last_status = 0.0
_status_shown = False


def status(msg, force=False):
    """Transient one-line progress on stderr (only on a terminal, throttled)."""
    global _last_status, _status_shown
    if not sys.stderr.isatty():
        return
    now = time.monotonic()
    if force or now - _last_status > 0.1:
        _last_status = now
        _status_shown = True
        sys.stderr.write("\r\033[K" + DIM(msg[:120]))
        sys.stderr.flush()


def clear_status():
    global _status_shown
    if _status_shown:
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
        _status_shown = False


def info(msg):
    clear_status()
    print(DIM(msg), flush=True)


# ---------------------------------------------------------------- config

def load_config(path):
    candidates = [path] if path else [
        "transmission-check.toml",
        os.path.expanduser("~/.config/transmission-check/config.toml"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            if tomllib is None:
                die("reading a config file needs Python >= 3.11 (or pass everything as options)")
            with open(p, "rb") as f:
                print(DIM(f"config: {p}"))
                return tomllib.load(f)
    if path:
        die(f"config file not found: {path}")
    return {}


def docker_mounts(container):
    """Bind mounts of a running container as {container_path: host_path}."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{json .Mounts}}", container],
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        die(f"`docker inspect {container}` failed: {getattr(e, 'stderr', e)}")
    return {m["Destination"]: m["Source"] for m in json.loads(out) if m.get("Type") == "bind"}


# ---------------------------------------------------------------- transmission rpc

def rpc(url, method, arguments, auth):
    session_id = ""
    for _ in range(2):
        req = urllib.request.Request(
            url, data=json.dumps({"method": method, "arguments": arguments}).encode()
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Transmission-Session-Id", session_id)
        if auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            if data.get("result") != "success":
                die(f"RPC error: {data.get('result')}")
            return data["arguments"]
        except urllib.error.HTTPError as e:
            if e.code == 409:  # CSRF handshake: retry with the id Transmission gives us
                session_id = e.headers.get("X-Transmission-Session-Id", "")
                continue
            if e.code == 401:
                die("401 Unauthorized: set user/password (config, options or env)")
            raise
        except urllib.error.URLError as e:
            die(f"cannot reach Transmission at {url}: {e.reason}")
    die("session-id handshake failed")


# ---------------------------------------------------------------- scanning

def to_host(path, mappings):
    """Container path -> host path using the longest matching prefix."""
    for cont, host in sorted(mappings.items(), key=lambda m: -len(m[0])):
        if path == cont or path.startswith(cont.rstrip("/") + "/"):
            return os.path.normpath(host + path[len(cont):])
    return None


def size_of(path):
    if os.path.isfile(path) or os.path.islink(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0
    total = n = 0
    for root, _, files in os.walk(path):
        for f in files:
            n += 1
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
        status(f"  measuring {path}: {n} files, {human(total)} so far")
    return total


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def find_orphans(root, known, ancestors, ignore, counter, skip=()):
    """Yield orphans under root: anything neither a known torrent path nor a parent of one."""
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as e:
        clear_status()
        print(YELLOW(f"  ! cannot read {root}: {e}"), file=sys.stderr)
        return
    for e in entries:
        counter[0] += 1
        status(f"  scanned {counter[0]} entries... {e.path}")
        if e.name in ignore:
            continue
        p = os.path.normpath(e.path)
        if p in skip:
            continue
        if p.endswith(".part"):  # transmission's "rename partial files"
            p = p[:-5]
        if p in known:
            continue
        if p in ancestors and e.is_dir(follow_symlinks=False):
            yield from find_orphans(e.path, known, ancestors, ignore, counter, skip)
            continue
        yield e.path


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  %(prog)s --docker-container transmission\n"
               "  %(prog)s --url http://brownfunk.lan:9091/transmission/rpc -u me "
               "--map /downloads=/mnt/t/downloads --map /data=/mnt/t/data\n"
               "  %(prog)s --config ~/my.toml --rm-list",
    )
    ap.add_argument("--config", metavar="FILE", help="TOML config file (see config.example.toml)")
    ap.add_argument("--url", help="Transmission RPC URL (default http://localhost:9091/transmission/rpc)")
    ap.add_argument("-u", "--user", help="RPC user (or env TRANSMISSION_USER)")
    ap.add_argument("-p", "--password", help="RPC password (or env TRANSMISSION_PASS; prefer env/config over the command line)")
    ap.add_argument("--map", action="append", metavar="CONTAINER=HOST",
                    help="container->host path mapping, repeatable; replaces the config's [paths]")
    ap.add_argument("--docker-container", metavar="NAME",
                    help="derive mappings from this container's bind mounts via `docker inspect`")
    ap.add_argument("--scan", action="append", metavar="CONTAINER_DIR",
                    help="container dir(s) to scan, repeatable (default: every mapped dir)")
    ap.add_argument("--exclude", action="append", metavar="CONTAINER_DIR",
                    help="container dir(s) never scanned or reported, repeatable: a whole mount (/config) "
                         "or a subfolder (/data/MUSIC_TO_BEET_IMPORT). With --docker-container, "
                         "only mounts that hold downloads are scanned anyway")
    ap.add_argument("--ignore", action="append", metavar="NAME",
                    help="entry NAME to skip at any depth, repeatable (e.g. @eaDir); use --exclude for a specific path")
    ap.add_argument("--rm-list", action="store_true", help="also print an `rm -rf` block (not executed)")
    args = ap.parse_args()

    cfg = load_config(args.config)

    url = args.url or cfg.get("url") or "http://localhost:9091/transmission/rpc"
    user = args.user or os.environ.get("TRANSMISSION_USER") or cfg.get("user")
    password = args.password or os.environ.get("TRANSMISSION_PASS") or cfg.get("password")
    scan = args.scan or cfg.get("scan")
    ignore = set(args.ignore or cfg.get("ignore", [])) | {"lost+found", ".stfolder"}
    container = args.docker_container or cfg.get("docker_container")

    auto_mappings = False
    if args.map:
        mappings = {}
        for m in args.map:
            cont, sep, host = m.partition("=")
            if not sep or not host:
                die(f"bad --map '{m}', expected CONTAINER=HOST")
            mappings[cont] = host
    elif cfg.get("paths"):
        mappings = dict(cfg["paths"])
    elif container:
        mappings = docker_mounts(container)
        auto_mappings = True
    else:
        die("no path mapping. Give --map, a [paths] table in the config, or --docker-container "
            "(see --help)")
    mappings = {(k.rstrip("/") or "/"): os.path.expanduser(v) for k, v in mappings.items()}

    auth = f"{user}:{password}" if user else None
    info(f"Querying Transmission RPC at {url} ...")
    session = rpc(url, "session-get", {}, auth)
    info(f"  connected (Transmission {session.get('version', '?')}); fetching torrent list ...")
    torrents = rpc(url, "torrent-get", {"fields": ["name", "downloadDir"]}, auth)["torrents"]
    print(BOLD(f"{len(torrents)} torrents known to Transmission"), flush=True)
    info("Path mapping (container -> host):")
    for cont, host in sorted(mappings.items()):
        info(f"  {cont} -> {host}" + ("  [excluded]" if any(cont == x or cont.startswith(x.rstrip('/') + '/') for x in (args.exclude or cfg.get('exclude', []))) else ""))

    known, ancestors, unmapped = set(), set(), set()
    for t in torrents:
        host = to_host(os.path.join(t["downloadDir"], t["name"]), mappings)
        if host is None:
            unmapped.add(t["downloadDir"])
            continue
        known.add(host)
        parent = os.path.dirname(host)
        while parent not in ancestors and parent != "/":
            ancestors.add(parent)
            parent = os.path.dirname(parent)
    for d in sorted(unmapped):
        print(YELLOW(f"  ! no mapping covers {d}: its torrents are ignored"))

    exclude = [x.rstrip("/") for x in (args.exclude or cfg.get("exclude", []))]

    def excluded(cont):
        return any(cont == x or cont.startswith(x + "/") for x in exclude)

    if scan:
        roots = sorted({h for h in (to_host(x, mappings) for x in scan) if h and os.path.isdir(h)})
    else:
        candidates = {cont: host for cont, host in mappings.items() if not excluded(cont)}
        if auto_mappings:
            # docker mounts include /config, /watch, ...: only keep mounts that hold downloads
            used = {t["downloadDir"] for t in torrents} | {session.get("download-dir")}
            if session.get("incomplete-dir-enabled"):
                used.add(session.get("incomplete-dir"))
            candidates = {cont: host for cont, host in candidates.items()
                          if any(u and (u == cont or u.startswith(cont.rstrip("/") + "/")) for u in used)}
        roots = sorted({h for h in candidates.values() if os.path.isdir(h)})
    if session.get("incomplete-dir-enabled"):
        inc = to_host(session["incomplete-dir"], mappings)
        if inc and os.path.isdir(inc) and not any(inc == r or inc.startswith(r + "/") for r in roots):
            roots.append(inc)
    if not roots:
        die("none of the mapped host folders exist; check your mappings")

    skip = {h for h in (to_host(x, mappings) for x in exclude) if h}  # excluded subfolders
    all_orphans = []
    for root in roots:
        clear_status()
        print(f"\n{BLUE('Scanning')} {root}", flush=True)
        counter, found = [0], 0
        for o in find_orphans(root, known, ancestors, ignore, counter, skip):
            found += 1
            kind = "dir " if os.path.isdir(o) else "file"
            sz = size_of(o)
            all_orphans.append((o, sz))
            clear_status()
            print(f"  {RED('✗')} {YELLOW(kind)} {o}  {DIM('(' + human(sz) + ')')}", flush=True)
        clear_status()
        print(DIM(f"  {counter[0]} entries checked, {found} orphan(s)") if found else
              GREEN(f"  ✓ nothing orphaned ({counter[0]} entries checked)"), flush=True)

    total = sum(s for _, s in all_orphans)
    print()
    if all_orphans:
        print(RED(BOLD(f"{len(all_orphans)} orphan(s), {human(total)} reclaimable")) + DIM(" — review before deleting"))
        if args.rm_list:
            print(DIM("\n# review, then run:"))
            for o, _ in all_orphans:
                print("rm -rf -- '" + o.replace("'", "'\\''") + "'")
    else:
        print(GREEN(BOLD("All clean: every file is tracked by Transmission")))


if __name__ == "__main__":
    main()
