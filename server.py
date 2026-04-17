#!/usr/bin/env python3
"""Claude Code Usage Dashboard — Server

Usage:
  python3 server.py              # http://localhost:8765
  python3 server.py 9000         # custom port
  python3 server.py --no-open    # don't open browser
"""
import json, os, glob, sys, time, webbrowser, tempfile, threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

CLAUDE_DIR = os.environ.get("CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
ANON = os.environ.get("CC_USAGE_ANON", "").lower() in ("1", "true", "yes")
CACHE_FILE = os.path.join(DATA_DIR, "usage.json")
PORT = 8765
CACHE_LOCK = threading.Lock()
CACHE_MISS_LOCK = threading.Lock()
CACHE_BODY = None

RATES_FILE = os.path.join(BASE_DIR, "rates.json")
with open(RATES_FILE) as f:
    RATES = json.load(f)


def get_rates(model_id):
    best_key, best_len = None, 0
    for key in RATES:
        if key in model_id and len(key) > best_len:
            best_key, best_len = key, len(key)
    return RATES[best_key] if best_key else None


def norm_model(m):
    for k, v in [('opus-4-7','opus-4-7'),('opus-4-6','opus-4-6'),('opus-4-5','opus-4-5'),
                 ('sonnet-4-6','sonnet-4-6'),('sonnet-4-5','sonnet-4-5'),
                 ('haiku-4-5','haiku-4-5'),('haiku-3-5','haiku-3-5')]:
        if k in m: return v
    return m


_ANON_MAP = {}
_ANON_COUNTER = [0]
_ANON_NAMES = ['nebula','pulsar','quasar','vortex','nova','cosmos','zenith','aurora',
               'helix','photon','prism','orbit','comet','solaris','eclipse','vertex',
               'cipher','axiom','nexus','arc','flux','ion','void','apex']
_NAME_CACHE = {}


def _decode_dir(encoded_dir):
    """Decode encoded project dir to filesystem basename by walking real FS."""
    encoded = encoded_dir.strip('-')
    if not encoded:
        return None

    def walk(current, remaining):
        if not remaining:
            return os.path.basename(current) if current != '/' else None
        try:
            entries = sorted(os.listdir(current), key=len, reverse=True)
        except OSError:
            return None
        for entry in entries:
            if entry.startswith('.'):
                continue
            # Claude Code encodes both / and spaces as -
            enc = entry.replace(' ', '-')
            if remaining == enc:
                return entry
            if remaining.startswith(enc + '-'):
                result = walk(os.path.join(current, entry), remaining[len(enc) + 1:])
                if result is not None:
                    return result
        return None

    return walk('/', encoded)


def _home_prefix():
    home = os.environ.get("CC_USAGE_HOME", os.path.expanduser("~"))
    return home.replace(os.sep, '-').strip('-') + '-'


_HOME_PFX = _home_prefix()


def _cwd_from_jsonl(proj_dir):
    """Extract cwd from first JSONL entry that has it."""
    for fp in glob.glob(f"{proj_dir}/*.jsonl")[:3]:
        try:
            with open(fp, errors='replace') as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        cwd = obj.get('cwd', '')
                        if cwd and cwd != '/':
                            return os.path.basename(cwd)
                    except Exception:
                        pass
        except Exception:
            pass
    return None


def proj_name(d):
    if d in _NAME_CACHE:
        return _NAME_CACHE[d]

    # Try filesystem-based decoding (works on native, fails in Docker)
    decoded = _decode_dir(d)
    if decoded:
        name = decoded
    else:
        # Try extracting project name from cwd field in JSONL logs
        proj_dir = os.path.join(CLAUDE_DIR, d)
        cwd_name = _cwd_from_jsonl(proj_dir) if os.path.isdir(proj_dir) else None
        if cwd_name:
            name = cwd_name
        else:
            # Final fallback: strip home prefix
            n = d.strip('-')
            if n.startswith(_HOME_PFX):
                n = n[len(_HOME_PFX):]
            name = n

    name = name[:50]
    if ANON:
        if name not in _ANON_MAP:
            idx = _ANON_COUNTER[0]
            _ANON_MAP[name] = _ANON_NAMES[idx % len(_ANON_NAMES)] if idx < len(_ANON_NAMES) else f'project-{idx}'
            _ANON_COUNTER[0] += 1
        name = _ANON_MAP[name]

    _NAME_CACHE[d] = name
    return name


def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None


def assistant_snapshot_rank(obj):
    """Rank duplicate assistant snapshots, preferring the most complete one."""
    msg = obj.get('message', {})
    usage = msg.get('usage', {})
    content = msg.get('content', [])
    stage = 0
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get('type')
        if t == 'thinking':
            stage = max(stage, 1)
        elif t == 'text':
            stage = max(stage, 2)
        elif t == 'tool_use':
            stage = max(stage, 3)
    return (
        obj.get('timestamp', ''),
        usage.get('output_tokens', 0),
        usage.get('cache_creation_input_tokens', 0),
        usage.get('cache_read_input_tokens', 0),
        usage.get('input_tokens', 0),
        stage,
    )


def read_cache_body():
    global CACHE_BODY
    with CACHE_LOCK:
        if CACHE_BODY is not None:
            return CACHE_BODY
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, 'rb') as f:
            CACHE_BODY = f.read()
        return CACHE_BODY


def write_cache_body(body):
    global CACHE_BODY
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix='usage.', suffix='.json')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(body)
        os.replace(tmp_path, CACHE_FILE)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    with CACHE_LOCK:
        CACHE_BODY = body


def get_or_build_cache_body():
    body = read_cache_body()
    if body is not None:
        return body
    with CACHE_MISS_LOCK:
        body = read_cache_body()
        if body is not None:
            return body
        print("No cache, extracting...")
        data = extract_data()
        body = json.dumps(data).encode()
        write_cache_body(body)
        return body


def extract_data():
    t0 = time.time()
    if not os.path.isdir(CLAUDE_DIR):
        return {'costs': {}, 'sessions': {}, 'tools': {}, 'compacts': {}}

    costs = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0])))
    costs_hourly = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0]))))  # day->hour->proj->model
    sessions = defaultdict(lambda: defaultdict(int))
    tools = defaultdict(lambda: defaultdict(int))
    tools_hourly = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))  # day->hour->proj->count
    tool_names = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    rate_events = []
    per_session_cost = defaultdict(float)
    per_session_first_day = {}

    for proj_dir in sorted(glob.glob(f"{CLAUDE_DIR}/*/")):
        pname = proj_name(os.path.basename(proj_dir.rstrip('/')))
        proj_path = Path(proj_dir)
        assistant_latest = {}

        for fp in glob.glob(f"{proj_dir}*.jsonl"):
            first_ts = None
            try:
                with open(fp, errors='replace') as f:
                    for line in f:
                        try:
                            obj = json.loads(line.strip())
                            if obj.get('timestamp'):
                                first_ts = obj['timestamp'][:10]
                                break
                        except Exception:
                            pass
            except Exception:
                pass
            if first_ts:
                sessions[first_ts][pname] += 1

        all_files = (glob.glob(f"{proj_dir}*.jsonl") +
                     glob.glob(f"{proj_dir}*/subagents/*.jsonl"))
        for fp in all_files:
            try:
                rel = Path(fp).relative_to(proj_path)
                parent_id = rel.parts[0].replace('.jsonl', '')
            except Exception:
                parent_id = os.path.basename(fp).replace('.jsonl', '')

            try:
                with open(fp, errors='replace') as f:
                    file_lines = f.readlines()
            except Exception:
                continue

            for line_no, raw_line in enumerate(file_lines, 1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except Exception:
                    continue

                ts = obj.get('timestamp', '')
                if not ts:
                    continue
                day = ts[:10]

                if obj.get('error') == 'rate_limit':
                    rate_events.append((ts, parent_id, pname))
                    continue

                t = obj.get('type', '')
                msg = obj.get('message', {})

                if t == 'assistant':
                    model_raw = msg.get('model', '')
                    if not model_raw or model_raw == '<synthetic>':
                        continue
                    usage = msg.get('usage', {})
                    if not usage:
                        continue
                    msg_id = msg.get('id') or obj.get('requestId') or f"{fp}:{line_no}"
                    # Dedup by msg_id alone: Anthropic msg.id is globally unique
                    # per API call, and /branch forks copy the same msg.id across
                    # session files under a new sessionId. Keying on sessionId too
                    # would count forked history once per fork.
                    rank = assistant_snapshot_rank(obj)
                    prev = assistant_latest.get(msg_id)
                    if prev is None or rank > prev[0]:
                        assistant_latest[msg_id] = (rank, obj, parent_id)

        for (_, obj, parent_id) in assistant_latest.values():
            session_key = obj.get('sessionId') or parent_id
            ts = obj.get('timestamp', '')
            if not ts:
                continue
            day = ts[:10]
            msg = obj.get('message', {})
            model_raw = msg.get('model', '')
            usage = msg.get('usage', {})
            it = usage.get('input_tokens', 0)
            ot = usage.get('output_tokens', 0)
            cr = usage.get('cache_read_input_tokens', 0)
            cw = usage.get('cache_creation_input_tokens', 0)
            # Split cache_write into 5m vs 1h TTL (priced 1.25× vs 2× base input).
            # Iterations are authoritative for multi-turn tool-use; top-level
            # cache_creation dict only reflects the last iteration.
            iters = usage.get('iterations') or []
            if iters:
                cw_5m = sum((x.get('cache_creation') or {}).get('ephemeral_5m_input_tokens', 0) for x in iters)
                cw_1h = sum((x.get('cache_creation') or {}).get('ephemeral_1h_input_tokens', 0) for x in iters)
            else:
                cc = usage.get('cache_creation') or {}
                cw_5m = cc.get('ephemeral_5m_input_tokens', 0)
                cw_1h = cc.get('ephemeral_1h_input_tokens', 0)
            cw_unclass = max(0, cw - cw_5m - cw_1h)  # attributed to 1h (Claude Code default)
            rates = get_rates(model_raw)
            cost = 0.0
            if rates:
                rate_1h = rates.get('cache_write_1h', rates['cache_write'])
                cost = (it * rates['input'] + ot * rates['output'] +
                        cr * rates['cache_read'] +
                        cw_5m * rates['cache_write'] +
                        (cw_1h + cw_unclass) * rate_1h) / 1e6
            model = norm_model(model_raw)
            v = costs[day][pname][model]
            v[0] += cost; v[1] += ot; v[2] += it; v[3] += cr; v[4] += cw

            hour = ts[11:13] if len(ts) > 12 else '00'
            vh = costs_hourly[day][hour][pname][model]
            vh[0] += cost; vh[1] += ot; vh[2] += it; vh[3] += cr; vh[4] += cw

            if cost > 0:
                skey = (session_key or parent_id, pname)
                per_session_cost[skey] += cost
                if skey not in per_session_first_day or day < per_session_first_day[skey]:
                    per_session_first_day[skey] = day

            for item in msg.get('content', []):
                if isinstance(item, dict) and item.get('type') == 'tool_use':
                    tools[day][pname] += 1
                    tools_hourly[day][hour][pname] += 1
                    tname = item.get('name', 'unknown')
                    tool_names[day][pname][tname] += 1

    # Dedup rate limit events by parent session (30-min window)
    rate_events.sort()
    seen = {}
    compacts = defaultdict(lambda: defaultdict(int))
    compact_times = []  # [(timestamp, project), ...]
    for ts, pid, proj in rate_events:
        if pid in seen:
            last = parse_iso(seen[pid])
            curr = parse_iso(ts)
            if last and curr and (curr - last).total_seconds() < 1800:
                continue
        seen[pid] = ts
        compacts[ts[:10]][proj] += 1
        compact_times.append((ts, proj))

    # Aggregate peak session cost per day per project
    peak_session = defaultdict(lambda: defaultdict(float))
    for (pid, proj), cost in per_session_cost.items():
        d = per_session_first_day.get((pid, proj))
        if d:
            peak_session[d][proj] = max(peak_session[d][proj], cost)

    result = {'costs': {}, 'costs_hourly': {}, 'sessions': {}, 'tools': {}, 'tools_hourly': {}, 'tool_names': {}, 'compacts': {}, 'compact_times': [], 'peak_session': {}}

    for day, projs in sorted(costs.items()):
        result['costs'][day] = {}
        for proj, models in projs.items():
            result['costs'][day][proj] = {}
            for model, v in models.items():
                result['costs'][day][proj][model] = {
                    'cost': round(v[0], 4), 'output': v[1], 'input': v[2],
                    'cache_read': v[3], 'cache_write': v[4]
                }

    for day, hours in sorted(costs_hourly.items()):
        result['costs_hourly'][day] = {}
        for hour, projs in sorted(hours.items()):
            result['costs_hourly'][day][hour] = {}
            for proj, models in projs.items():
                result['costs_hourly'][day][hour][proj] = {}
                for model, v in models.items():
                    result['costs_hourly'][day][hour][proj][model] = {
                        'cost': round(v[0], 4), 'output': v[1], 'input': v[2],
                        'cache_read': v[3], 'cache_write': v[4]
                    }

    for day, projs in sorted(sessions.items()):
        result['sessions'][day] = dict(projs)
    for day, projs in sorted(tools.items()):
        result['tools'][day] = dict(projs)
    for day, hours in sorted(tools_hourly.items()):
        result['tools_hourly'][day] = {h: dict(projs) for h, projs in sorted(hours.items())}
    for day, projs in sorted(tool_names.items()):
        result['tool_names'][day] = {p: dict(names) for p, names in projs.items()}
    for day, projs in sorted(compacts.items()):
        result['compacts'][day] = dict(projs)
    result['compact_times'] = [{'ts': ts, 'project': proj} for ts, proj in sorted(compact_times, reverse=True)]

    for day, projs in sorted(peak_session.items()):
        result['peak_session'][day] = {p: round(c, 2) for p, c in projs.items()}

    elapsed = time.time() - t0
    n_days = len(result['costs'])
    n_lim = sum(v for projs in result['compacts'].values() for v in projs.values())
    print(f"  Extracted in {elapsed:.1f}s — {n_days} days, {n_lim} limit hits")
    return result


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/data':
            body = get_or_build_cache_body()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(body)
        elif path == '/api/refresh':
            print("Refreshing data...")
            data = extract_data()
            body = json.dumps(data).encode()
            write_cache_body(body)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ('/', ''):
            self.path = '/index.html'
            super().do_GET()
        else:
            super().do_GET()

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    flags = {a for a in sys.argv[1:] if a.startswith('-')}
    port = int(args[0]) if args else PORT
    no_open = '--no-open' in flags

    os.makedirs(DATA_DIR, exist_ok=True)
    os.chdir(BASE_DIR)

    url = f"http://localhost:{port}"
    server = ThreadingHTTPServer(('', port), Handler)
    print(f"Dashboard → {url}")

    if not no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
