#!/usr/bin/env python3
# <xbar.title>AI pets</xbar.title>
# <xbar.version>3.0</xbar.version>
# <xbar.desc>Your Claude Code and Codex accounts (via aisw) as pixel pets whose life is the quota left. Switch, add and remove accounts.</xbar.desc>
# <xbar.dependencies>python3, aisw, aiswitch</xbar.dependencies>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.type>streamable</swiftbar.type>
"""
SwiftBar plugin (streamable, so the pets can move). Drawing only reads a local
cache; quota is fetched by this same file running in the background ("fetch").

Read-only by design:
  * it never refreshes or rewrites any login (refresh tokens are single-use,
    so doing that would log Claude Code / Codex out),
  * it never sends prompts with your subscription tokens,
  * tokens are never written to the cache, logs or command lines.

Data sources
  Claude Code : GET api.anthropic.com/api/oauth/usage (undocumented; polled
                sparingly because it rate-limits hard)
  Codex       : GET chatgpt.com/backend-api/wham/usage, else `codex app-server`
                (account/rateLimits/read), else the last session log.
"""
import datetime
import fcntl
import glob
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse

HOME = os.path.expanduser("~")
# Menu-bar apps don't get your Terminal's PATH.
os.environ["PATH"] = ":".join([
    os.path.join(HOME, ".local/bin"), os.path.join(HOME, ".cargo/bin"),
    "/opt/homebrew/bin", "/usr/local/bin",
    os.path.join(HOME, "anaconda3/bin"), os.path.join(HOME, "miniconda3/bin"),
    "/usr/bin", "/bin", "/usr/sbin", "/sbin", os.environ.get("PATH", ""),
])
# Often the only Codex CLI is the one bundled with the VS Code ChatGPT extension; aisw needs it on PATH too.
if not any(d and os.access(os.path.join(d, "codex"), os.X_OK) for d in os.environ["PATH"].split(":")):
    _bundled = glob.glob(os.path.join(HOME, ".vscode", "extensions", "openai.chatgpt-*", "bin", "*", "codex"))
    if _bundled:
        os.environ["PATH"] += ":" + os.path.dirname(max(_bundled, key=os.path.getmtime))

SELF = os.path.abspath(__file__)
PLUGIN_NAME = os.path.basename(SELF).split(".")[0]
AISW_HOME = os.environ.get("AISW_HOME") or os.path.join(HOME, ".aisw")
DATA_DIR = os.environ.get("AISWITCH_WIDGET_DIR") or os.path.join(HOME, "Library", "Caches", "aiswitch-widget")
CACHE_FILE = os.path.join(DATA_DIR, "quota.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOCK_FILE = os.path.join(DATA_DIR, "fetch.lock")

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# How often to ask for fresh numbers, in seconds: (tool, is_active_account)
POLL = {("claude", True): 10 * 60, ("claude", False): 20 * 60,
        ("codex", True): 3 * 60, ("codex", False): 10 * 60}
ALERT_AT = 80

TOOLS = [("claude", "Claude Code", "✳"), ("codex", "Codex", "◎")]
GREEN, AMBER, RED, GREY = "#34C759,#30D158", "#FF9F0A,#FFB340", "#FF3B30,#FF6961", "#8E8E93,#98989D"


# ── small helpers ────────────────────────────────────────────────────────────
def now():
    return time.time()


def run(cmd, stdin=None, timeout=20, env=None):
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def settings():
    s = {"alerts": True, "animate": True}
    s.update(read_json(SETTINGS_FILE, {}))
    return s


def parse_time(v):
    """ISO string or epoch (s or ms) -> epoch seconds, or None."""
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e11 else float(v)
    try:
        s = str(v).replace("Z", "+00:00")
        # Python 3.9 wants exactly 6 fractional digits
        s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), s)
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def jwt_claims(token):
    try:
        import base64
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def find_key(obj, names):
    """First value for any of `names` anywhere inside nested JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in names and v not in (None, ""):
                return v
        for v in obj.values():
            r = find_key(v, names)
            if r not in (None, ""):
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, names)
            if r not in (None, ""):
                return r
    return None


def http_get(url, headers, timeout=15):
    """GET via macOS curl. Headers (incl. the token) go through stdin, never argv."""
    cfg = "".join('header = "%s: %s"\n' % (k, str(v).replace('"', "")) for k, v in headers.items())
    fd, hdr_file = tempfile.mkstemp()
    os.close(fd)
    try:
        rc, out, err = run(["curl", "-sS", "-m", str(timeout), "-K", "-", "-D", hdr_file,
                            "-w", "\n%{http_code}", url], stdin=cfg, timeout=timeout + 5)
        with open(hdr_file) as f:
            raw_headers = f.read()
    finally:
        os.unlink(hdr_file)
    body, _, code = out.rpartition("\n")
    hdrs = {}
    for line in raw_headers.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    try:
        code = int(code)
    except ValueError:
        code = 0
    return code, body, hdrs, (err.strip() if rc else "")


def notify(title, body):
    run(["osascript", "-e", "on run a", "-e",
         "display notification (item 2 of a) with title (item 1 of a)", "-e", "end run", title, body], timeout=5)


def window_label(seconds):
    if not seconds:
        return "Window"
    return {18000: "5h", 604800: "Week", 2592000: "Month"}.get(int(seconds), "%dh" % round(seconds / 3600.0))


def win(label, pct, reset=None, dur=None, scoped=False):
    """One quota window. pct = percent USED (0-100), as the services report it."""
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return None
    w = {"label": label, "pct": max(0.0, pct), "reset": parse_time(reset), "dur": dur}
    if scoped:
        w["scoped"] = True  # only limits one model/feature, not the whole account
    return w


def short_name(name):
    """'GPT-5.3-Codex-Spark' -> 'Spark'."""
    name = str(name).strip()
    return name.split("-")[-1] if len(name) > 8 and "-" in name else name[:8]


def scoped_label(name, secs):
    return "%s %s" % (short_name(name), {604800: "wk", 18000: "5h", 2592000: "mo"}.get(int(secs or 0), window_label(secs)))


# ── aisw profiles ────────────────────────────────────────────────────────────
def profiles():
    rc, out, _ = run(["aisw", "list", "--json"], timeout=10)
    try:
        data = json.loads(out)
    except ValueError:
        return None
    result = []
    for tool, _, _ in TOOLS:
        t = data.get(tool) or {}
        for p in t.get("profiles", []):
            auth = (p.get("auth") or "") + " " + str(p.get("%s_auth_classification" % tool) or "")
            result.append({
                "tool": tool, "name": p["name"], "label": p.get("label") or "",
                "active": p["name"] == t.get("active"),
                "api_key": "api_key" in auth or "apikey" in auth.replace("_", ""),
                "dir": os.path.join(AISW_HOME, "profiles", tool, p["name"]),
            })
    return result


def key_of(p):
    return "%s:%s" % (p["tool"], p["name"])


# ── Claude Code ──────────────────────────────────────────────────────────────
def keychain_read(service):
    rc, out, _ = run(["security", "find-generic-password", "-s", service, "-w"], timeout=8)
    if rc != 0:
        return None
    out = out.strip()
    if out and not out.startswith("{") and re.fullmatch(r"[0-9a-fA-F]+", out):
        try:  # `security -w` prints hex when the stored value contains a newline
            out = bytes.fromhex(out).decode("utf-8", "replace").strip()
        except ValueError:
            pass
    return out


def claude_credentials(pdir):
    h = hashlib.sha256(unicodedata.normalize("NFC", pdir).encode()).hexdigest()[:8]
    found = []
    raws = [keychain_read("Claude Code-credentials-" + h)]
    try:
        with open(os.path.join(pdir, ".credentials.json")) as f:
            raws.append(f.read())
    except OSError:
        pass
    for raw in raws:
        try:
            o = json.loads(raw or "").get("claudeAiOauth") or {}
        except (ValueError, AttributeError):
            continue
        if o.get("accessToken"):
            found.append(o)
    return max(found, key=lambda o: o.get("expiresAt") or 0) if found else None


def claude_plan(c):
    tier = (c.get("rateLimitTier") or "").lower()
    m = re.search(r"max_(\d+)x", tier)
    if m:
        return "Max %sx" % m.group(1)
    sub = (c.get("subscriptionType") or "").strip()
    return sub[:1].upper() + sub[1:] if sub else "Claude"


_claude_version = None


def claude_ua():
    global _claude_version
    if _claude_version is None:
        _, out, _ = run(["claude", "--version"], timeout=10)
        m = re.search(r"\d+\.\d+\.\d+", out)
        _claude_version = m.group(0) if m else "2.1.280"
    return "claude-code/" + _claude_version


def parse_claude_usage(d):
    wins = {}

    def put(key, w):
        if w:
            if not w["reset"] and key in wins:
                w["reset"] = wins[key]["reset"]
            wins[key] = w

    fh, sd = d.get("five_hour") or {}, d.get("seven_day") or {}
    if fh.get("utilization") is not None:
        put("5h", win("5h", fh["utilization"], fh.get("resets_at"), 18000))
    if sd.get("utilization") is not None:
        put("Week", win("Week", sd["utilization"], sd.get("resets_at"), 604800))
    for k, lab in (("seven_day_opus", "Opus wk"), ("seven_day_sonnet", "Sonnet wk")):
        v = d.get(k) or {}
        if v.get("utilization") is not None:
            put(lab, win(lab, v["utilization"], v.get("resets_at"), 604800, scoped=True))
    for lim in d.get("limits") or []:  # newer shape; wins over the top-level keys
        pct = lim.get("percent", lim.get("utilization"))
        kind = lim.get("kind")
        if pct is None:
            continue
        if kind == "session":
            put("5h", win("5h", pct, lim.get("resets_at"), 18000))
        elif kind == "weekly_all":
            put("Week", win("Week", pct, lim.get("resets_at"), 604800))
        elif kind == "weekly_scoped":
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name") or lim.get("group") or "Model"
            lab = scoped_label(model, 604800)
            put(lab, win(lab, pct, lim.get("resets_at"), 604800, scoped=True))
    notes = []
    grants = [g for g in ((d.get("cedar_ember") or {}).get("grants") or []) if g.get("usable_now")]
    if grants:
        notes.append("🎟  %d saved limit reset%s ready to use" % (len(grants), "" if len(grants) == 1 else "s"))
    extra = d.get("extra_usage") or {}
    if extra.get("is_enabled"):
        used, cap = extra.get("used_credits"), extra.get("monthly_limit")
        notes.append("💳  Extra usage on" + (" · %s of %s used" % (used, cap) if used is not None and cap else ""))
    order = {"5h": 0, "Week": 1}
    return sorted(wins.values(), key=lambda w: (order.get(w["label"], 2), w["label"])), notes


def fetch_claude(p, old):
    if p["api_key"]:
        return {"status": "apikey", "plan": "API key"}
    c = claude_credentials(p["dir"])
    if not c:
        return {"status": "nologin", "error": "No saved login found for this profile"}
    plan = claude_plan(c)
    exp = (c.get("expiresAt") or 0) / 1000.0
    if exp and exp < now() + 60:
        return {"status": "expired", "plan": plan,
                "error": "Login needs a refresh — Wake it up below opens Claude Code on it"}
    code, body, hdrs, err = http_get(CLAUDE_USAGE_URL, {
        "Authorization": "Bearer " + c["accessToken"],
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": claude_ua(),
        "Accept": "application/json",
    })
    if code == 200:
        try:
            wins, notes = parse_claude_usage(json.loads(body))
        except ValueError:
            return {"status": "error", "plan": plan, "error": "Unexpected reply from Anthropic"}
        return {"status": "ok", "plan": plan, "windows": wins, "notes": notes, "source": "usage API"}
    if code == 429:
        try:
            wait = int(float(hdrs.get("retry-after", "0")))
        except ValueError:
            wait = 0
        return {"status": "busy", "plan": plan, "retry_after": wait,
                "error": "Anthropic asked us to slow down — showing the last numbers"}
    if code == 401:
        return {"status": "expired", "plan": plan, "error": "Login expired — Wake it up below opens Claude Code to sign in again"}
    if code == 403:
        return {"status": "error", "plan": plan, "error": "This login can't read usage (setup-token logins can't)"}
    return {"status": "error", "plan": plan, "error": err or "Usage check failed (HTTP %s)" % code}


# ── Codex ────────────────────────────────────────────────────────────────────
def codex_plan_name(v):
    v = str(v or "").strip().lower()
    if not v:
        return ""
    v = re.sub(r"^(self_serve_|chatgpt_)", "", v)
    words = {"prolite": "Pro Lite", "plus": "Plus", "pro": "Pro", "team": "Team", "business": "Business",
             "enterprise": "Enterprise", "edu": "Edu", "free": "Free", "go": "Go"}
    return " ".join(words.get(w, w.capitalize()) for w in v.split("_") if w)


def parse_wham(d):
    wins, notes = [], []
    rl = d.get("rate_limit") or d.get("rate_limits") or {}
    for k in ("primary_window", "secondary_window"):
        w = rl.get(k)
        if w and w.get("used_percent") is not None:
            secs = w.get("limit_window_seconds")
            reset = w.get("reset_at") or w.get("resets_at")
            if not reset and w.get("reset_after_seconds") is not None:
                reset = now() + float(w["reset_after_seconds"])
            wins.append(win(window_label(secs), w["used_percent"], reset, secs))
    for extra in d.get("additional_rate_limits") or []:
        name = extra.get("limit_name") or extra.get("metered_feature") or "Extra"
        erl = extra.get("rate_limit") or {}
        for k in ("primary_window", "secondary_window"):
            w = erl.get(k)
            if w and w.get("used_percent") is not None:
                secs = w.get("limit_window_seconds")
                reset = w.get("reset_at") or (now() + float(w["reset_after_seconds"]) if w.get("reset_after_seconds") is not None else None)
                wins.append(win(scoped_label(name, secs), w["used_percent"], reset, secs, scoped=True))
    n = (d.get("rate_limit_reset_credits") or {}).get("available_count")
    if n:
        notes.append("🎟  %d limit reset%s available" % (n, "" if n == 1 else "s"))
    cr = d.get("credits") or {}
    if cr.get("has_credits") and cr.get("balance") not in (None, "", "0", 0):
        notes.append("💳  Credits: %s" % cr.get("balance"))
    return [w for w in wins if w], notes, codex_plan_name(d.get("plan_type"))


def codex_app_server(pdir, timeout=25):
    """Ask Codex itself (documented JSON-RPC). Codex handles its own token refresh."""
    env = dict(os.environ, CODEX_HOME=pdir)
    try:
        proc = subprocess.Popen(["codex", "-s", "read-only", "-a", "never", "app-server"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1, env=env)
    except OSError:
        return None
    q = queue.Queue()

    def reader():
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(msg):
        try:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass

    replies, deadline = {}, now() + timeout
    send({"method": "initialize", "id": 1, "params": {"clientInfo": {"name": "aiswitch-widget", "title": "aiswitch widget", "version": "2.0"}}})
    sent_rest = False
    try:
        while now() < deadline and 3 not in replies:
            try:
                line = q.get(timeout=max(0.1, deadline - now()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                replies[msg["id"]] = msg
            if 1 in replies and not sent_rest:
                sent_rest = True
                send({"method": "initialized"})
                send({"method": "account/read", "id": 2, "params": {"refreshToken": False}})
                send({"method": "account/rateLimits/read", "id": 3})
    finally:
        proc.kill()
    return replies


def parse_app_server(replies):
    r3 = replies.get(3) or {}
    if "error" in r3:
        text = json.dumps(r3["error"])
        return None, "expired" if "401" in text or "unauthor" in text.lower() else "error"
    res = r3.get("result") or {}
    wins, notes = [], []
    rl = res.get("rateLimits") or {}
    for k in ("primary", "secondary"):
        w = rl.get(k)
        if w and w.get("usedPercent") is not None:
            secs = (w.get("windowDurationMins") or 0) * 60
            wins.append(win(window_label(secs), w["usedPercent"], w.get("resetsAt"), secs))
    for lid, other in (res.get("rateLimitsByLimitId") or {}).items():
        if lid == (rl.get("limitId") or "codex"):
            continue
        for k in ("primary", "secondary"):
            w = (other or {}).get(k)
            if w and w.get("usedPercent") is not None:
                secs = (w.get("windowDurationMins") or 0) * 60
                wins.append(win(scoped_label(other.get("limitName") or lid, secs), w["usedPercent"], w.get("resetsAt"), secs, scoped=True))
    n = (res.get("rateLimitResetCredits") or {}).get("availableCount")
    if n:
        notes.append("🎟  %d limit reset%s available" % (n, "" if n == 1 else "s"))
    plan = codex_plan_name(find_key(replies.get(2) or {}, ("planType", "plan_type")) or find_key(res, ("planType", "plan_type")))
    return {"windows": [w for w in wins if w], "notes": notes, "plan": plan}, "ok"


def codex_from_sessions(pdir):
    files = glob.glob(os.path.join(pdir, "sessions", "*", "*", "*", "rollout-*.jsonl"))
    files += glob.glob(os.path.join(pdir, "archived_sessions", "*.jsonl"))
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    for path in files[:8]:
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                j = json.loads(line)
            except ValueError:
                continue
            rl = find_key(j, ("rate_limits",))
            if not isinstance(rl, dict):
                continue
            ts = parse_time(j.get("timestamp")) or os.path.getmtime(path)
            wins = []
            for k in ("primary", "secondary"):
                w = rl.get(k)
                if not w or w.get("used_percent") is None:
                    continue
                secs = (w.get("window_minutes") or 0) * 60
                reset = w.get("resets_at") or None
                if not reset and w.get("resets_in_seconds") is not None:
                    reset = ts + float(w["resets_in_seconds"])
                wins.append(win(window_label(secs), w["used_percent"], reset, secs))
            if wins:
                return {"windows": [w for w in wins if w], "plan": codex_plan_name(rl.get("plan_type")), "as_of": ts}
    return None


def fetch_codex(p, old):
    if p["api_key"]:
        return {"status": "apikey", "plan": "API key"}
    auth = read_json(os.path.join(p["dir"], "auth.json"), {})
    if auth.get("OPENAI_API_KEY") and not (auth.get("tokens") or {}).get("access_token"):
        return {"status": "apikey", "plan": "API key"}
    tokens = auth.get("tokens") or {}
    token, account = tokens.get("access_token"), tokens.get("account_id")
    idc = jwt_claims(tokens.get("id_token") or "")
    plan = codex_plan_name(((idc.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")))
    exp = jwt_claims(token or "").get("exp") or 0

    # 1) the usage endpoint Codex itself polls, if the saved token is still valid
    if token and exp > now() + 60:
        hdr = {"Authorization": "Bearer " + token, "User-Agent": "codex-cli", "Accept": "application/json"}
        if account:
            hdr["ChatGPT-Account-Id"] = account
        code, body, hdrs, err = http_get(CODEX_USAGE_URL, hdr)
        if code == 200:
            try:
                wins, notes, p2 = parse_wham(json.loads(body))
                return {"status": "ok", "plan": p2 or plan, "windows": wins, "notes": notes, "source": "usage API"}
            except ValueError:
                pass
        if code == 429:
            return {"status": "busy", "plan": plan, "retry_after": 300,
                    "error": "OpenAI asked us to slow down — showing the last numbers"}
    # 2) ask Codex (it refreshes this profile's own login if it needs to)
    replies = codex_app_server(p["dir"])
    if replies and 3 in replies:
        data, state = parse_app_server(replies)
        if data:
            return {"status": "ok", "plan": data["plan"] or plan, "windows": data["windows"],
                    "notes": data["notes"], "source": "Codex"}
        if state == "expired":
            return {"status": "expired", "plan": plan, "error": "Login expired — Wake it up below opens Codex to sign in again"}
    # 3) whatever the last Codex session on this account recorded
    s = codex_from_sessions(p["dir"])
    if s:
        return {"status": "ok", "plan": s["plan"] or plan, "windows": s["windows"], "source": "last session",
                "as_of": s["as_of"]}
    if not token:
        return {"status": "nologin", "plan": plan, "error": "No saved login found for this profile"}
    return {"status": "error", "plan": plan, "error": "Couldn't read usage right now"}


# ── fetching (background) ────────────────────────────────────────────────────
def due(p, entry, t):
    if not entry:
        return True
    if entry.get("blocked_until", 0) > t:
        return False
    return t >= entry.get("fetched_at", 0) + POLL[(p["tool"], p["active"])]


def merge(old, new, t):
    """Keep the last good numbers when a check fails, so the menu never goes blank."""
    e = dict(old or {})
    e["checked_at"] = t
    for k in ("plan", "status", "error", "source", "notes"):
        if k in new:
            e[k] = new[k]
        elif k in ("error",):
            e.pop(k, None)
    if new.get("status") in ("ok",):
        e["windows"] = new.get("windows") or []
        e["fetched_at"] = t
        e["as_of"] = new.get("as_of", t)
        e["fails"] = 0
        e.pop("blocked_until", None)
        if "notes" not in new:
            e["notes"] = []
    elif new.get("status") in ("apikey", "expired", "nologin"):
        e["fetched_at"] = t
        e["windows"] = [] if new["status"] != "expired" else e.get("windows", [])
        e["fails"] = 0
    else:  # busy / error: back off, keep old numbers
        e["fails"] = e.get("fails", 0) + 1
        backoff = min(2 * 3600, 15 * 60 * (2 ** (e["fails"] - 1)))
        e["blocked_until"] = t + max(new.get("retry_after") or 0, backoff)
        e["fetched_at"] = e.get("fetched_at", 0)
    return e


def alert(p, old, new):
    if not settings().get("alerts"):
        return
    was = {w["label"]: w["pct"] for w in (old or {}).get("windows") or []}
    who = "%s %s" % (dict((t, n) for t, n, _ in TOOLS)[p["tool"]], p["name"])
    for w in new.get("windows") or []:
        before = was.get(w["label"])
        if before is None:
            continue
        if w["pct"] >= 100 > before:
            notify("😴 %s fell asleep" % who, "Its %s limit ran out. Wakes up %s." % (
                w["label"], fmt_reset(w["reset"]) if w["reset"] else "at the next reset"))
        elif w["pct"] >= ALERT_AT > before:
            notify("😰 %s is getting hungry" % who, "Only %d%% of the %s limit left · resets %s" % (
                left(w["pct"]), w["label"], fmt_reset(w["reset"]) if w["reset"] else "later"))
        elif before >= 100 > w["pct"]:
            notify("🌅 %s woke up!" % who, "The %s limit reset — ready to work." % w["label"])


def fetch(targets):
    os.makedirs(DATA_DIR, exist_ok=True)
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another fetch is already running
    profs = profiles() or []
    t = now()
    cache = read_json(CACHE_FILE, {})
    changed = False
    for p in profs:
        k = key_of(p)
        entry = cache.get(k)
        forced = targets == "all" or k in targets
        # a click skips our own error backoff, but not a service's "slow down"
        held = (entry or {}).get("blocked_until", 0) > t and (entry or {}).get("status") == "busy"
        if not (forced and not held) and not due(p, entry, t):
            continue
        new = (fetch_claude if p["tool"] == "claude" else fetch_codex)(p, entry)
        cache = read_json(CACHE_FILE, {})  # re-read: others may have written meanwhile
        if new.get("status") == "ok":
            alert(p, cache.get(k), new)
        cache[k] = merge(cache.get(k), new, now())
        write_json(CACHE_FILE, cache)
        changed = True
    if changed:
        run(["open", "-g", "swiftbar://refreshplugin?name=" + urllib.parse.quote(PLUGIN_NAME)], timeout=5)


def spawn_fetch(target="due"):
    subprocess.Popen([sys.executable, SELF, "fetch", target], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


# ── formatting ───────────────────────────────────────────────────────────────
def left(pct):
    return max(0, min(100, int(round(100 - pct))))


def color_for(pct):  # takes percent USED
    return GREEN if pct < 60 else AMBER if pct < 85 else RED


def bar(pct, width=12):
    """Battery-style bar: filled blocks = what's LEFT."""
    filled = int(round(left(pct) / 100.0 * width))
    return "▰" * filled + "▱" * (width - filled)


def fmt_dur(secs):
    secs = max(0, int(secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm" % max(m, 1)


def fmt_reset(ts):
    if not ts:
        return "—"
    left = ts - now()
    if left <= 0:
        return "now"
    if left < 24 * 3600:
        return "in " + fmt_dur(left)
    return datetime.datetime.fromtimestamp(ts).strftime("%a %H:%M")


def fmt_age(ts):
    if not ts:
        return "never"
    a = now() - ts
    return "just now" if a < 60 else fmt_dur(a) + " ago"


def pace(w):
    """Compare quota used with time elapsed in the window."""
    if not (w.get("reset") and w.get("dur")):
        return None
    elapsed = 1 - (w["reset"] - now()) / float(w["dur"])
    if elapsed <= 0.05 or elapsed > 1:
        return None
    ratio = (w["pct"] / 100.0) / elapsed
    if w["pct"] >= 100:
        return None
    if ratio > 1.25:
        # at this rate, when does it run out?
        runway = (100 - w["pct"]) / (w["pct"] / (elapsed * w["dur"])) if w["pct"] > 0 else None
        if runway is not None and w["reset"] - now() > runway:
            return "🔥 Burning fast — runs out in ~%s, before the reset" % fmt_dur(runway)
        return "🔥 Ahead of pace, but should last until the reset"
    if ratio < 0.6:
        return "🧊 Plenty of room — you're using it slowly"
    return "🙂 On pace"


def safe(s):
    return str(s).replace("|", "¦").replace("\n", " ")


def worst(entry, include_scoped=False):
    """The tightest window. Model-only limits don't count unless asked."""
    ws = (entry or {}).get("windows") or []
    main = [w for w in ws if include_scoped or not w.get("scoped")] or ws
    return max(main, key=lambda w: w["pct"]) if main else None




# ── pets ─────────────────────────────────────────────────────────────────────
# Original pixel pets. "#" outline, "." body (fills with life), letters = details.
SPRITES = {
    # Claude Code accounts: a round sprout ("Mochi")
    "claude": [
        "    lll  lll    ",
        "     llssll     ",
        "       ss       ",
        "    ########    ",
        "   #........#   ",
        "  #..........#  ",
        " #............# ",
        " #............# ",
        " #............# ",
        " #............# ",
        " #............# ",
        " #............# ",
        " #............# ",
        "  #..........#  ",
        "   ##########   ",
        "    ##    ##    ",
    ],
    # Codex accounts: a little robot ("Bit")
    "codex": [
        "       aa       ",
        "       ##       ",
        "   ##########   ",
        "  #..........#  ",
        "  #..........#  ",
        " b#..........#b ",
        " b#..........#b ",
        "  #..........#  ",
        "  #..........#  ",
        "  #..........#  ",
        "  #..........#  ",
        "  #..........#  ",
        "  #..........#  ",
        "   ##########   ",
        "    ##    ##    ",
        "    ##    ##    ",
    ],
    # no data yet: an egg that wobbles
    "egg": [
        "                ",
        "                ",
        "      ####      ",
        "     #....#     ",
        "    #..p...#    ",
        "   #........#   ",
        "   #.....p..#   ",
        "  #..........#  ",
        "  #.p........#  ",
        "  #......p...#  ",
        "  #..........#  ",
        "  #...p......#  ",
        "   #........#   ",
        "    ########    ",
        "                ",
        "                ",
    ],
}
FACE = {"claude": {"eye": 7, "mouth": 10, "cols": (5, 10), "blush": 9},
        "codex": {"eye": 6, "mouth": 9, "cols": (5, 10), "blush": 8}}

INK = (43, 35, 32, 255)          # outline / eyes
CREAM = (255, 246, 229, 255)     # empty body
SLEEPY = (214, 208, 199, 255)    # body while asleep
LIFE = {"high": ((76, 217, 100), (150, 240, 165)), "mid": ((255, 176, 32), (255, 214, 120)),
        "low": ((255, 77, 77), (255, 150, 150)), "gold": ((255, 204, 0), (255, 232, 130))}
DETAIL = {"l": (61, 190, 90, 255), "s": (40, 140, 65, 255), "b": (150, 150, 160, 255),
          "p": (240, 190, 150, 255)}
BLUSH = (255, 143, 163, 255)
SWEAT = (90, 200, 250, 255)
Z = ["###", "  #", " # ", "###"]          # asleep: limit ran out
QMARK = ["## ", "  #", " # ", "   ", " # "]  # snoozing: login needs a refresh


def mood_of(entry):
    """-> (mood, life 0-100). Moods: egg, sleep, snooze, rich, thriving, content, hungry, exhausted."""
    e = entry or {}
    st = e.get("status")
    if st == "apikey":
        return "rich", 100
    w = worst(e)
    if st in ("expired", "nologin"):
        return "snooze", left(w["pct"]) if w else 0
    if not w:
        return "egg", None
    life = left(w["pct"])
    if w["pct"] >= 100:
        return "sleep", 0
    if life >= 60:
        return "thriving", life
    if life >= 30:
        return "content", life
    if life >= 10:
        return "hungry", life
    return "exhausted", life


MOOD_TEXT = {"thriving": "😊 Thriving", "content": "🙂 Doing fine", "hungry": "😰 Getting hungry",
             "exhausted": "🥵 Exhausted", "sleep": "😴 Asleep until the limit resets",
             "snooze": "💤 Snoozing — needs you to log in again", "egg": "🥚 Hatching… (checking quota)",
             "rich": "🪙 Well fed — pay-as-you-go API key"}


def mood_text(p, entry):
    mood, _ = mood_of(entry)
    if mood == "snooze" and (entry or {}).get("status") == "expired":
        return "💤 Snoozing — open %s to wake it" % dict((t, n) for t, n, _ in TOOLS)[p["tool"]]
    return MOOD_TEXT[mood]


def draw_pet(species, mood, life, frame=0):
    """-> 16x17 grid of RGBA tuples (None = transparent)."""
    W, H = 16, 17
    g = [[None] * W for _ in range(H)]
    if mood == "egg":
        sprite, dx, dy = SPRITES["egg"], (0, 1, 0, -1)[frame % 4] if frame % 8 < 4 else 0, 1
    else:
        sprite, dx = SPRITES[species], 0
        bounce = {"thriving": 2, "content": 4, "rich": 3}.get(mood)
        dy = 1 - ((frame // bounce) % 2) if bounce else 1
    cells = [(r, c) for r, row in enumerate(sprite) for c, ch in enumerate(row) if ch == "."]
    top = min(r for r, _ in cells)
    bottom = max(r for r, _ in cells)
    rows = bottom - top + 1
    if mood in ("sleep", "snooze", "egg") or life is None:
        level = 0
    else:
        level = max(1, int(round(life / 100.0 * rows))) if life > 0 else 0
    tone = "gold" if mood == "rich" else "high" if (life or 0) >= 60 else "mid" if (life or 0) >= 30 else "low"
    base, light = LIFE[tone]
    body = SLEEPY if mood in ("sleep", "snooze") else CREAM

    def put(r, c, col):
        r2, c2 = r + dy, c + dx
        if 0 <= r2 < H and 0 <= c2 < W:
            g[r2][c2] = col

    for r, row in enumerate(sprite):
        for c, ch in enumerate(row):
            if ch == "#":
                put(r, c, INK)
            elif ch == ".":
                if level and r > bottom - level:
                    surface = r == bottom - level + 1
                    ripple = surface and (c + frame) % 3 == 0
                    put(r, c, (light if ripple else base) + (255,))
                else:
                    put(r, c, body)
            elif ch == "a":  # antenna light pulses
                put(r, c, (255, 107, 107, 255) if frame % 4 < 2 or mood in ("sleep", "snooze") else (255, 190, 90, 255))
            elif ch in DETAIL:
                put(r, c, DETAIL[ch])
    if mood == "egg":
        return g

    f = FACE[species]
    e, m, (c1, c2) = f["eye"], f["mouth"], f["cols"]
    blink = frame % 14 == 0
    if mood in ("sleep", "snooze", "exhausted") or blink:
        for c in (c1 - 1, c1, c2, c2 + 1):
            put(e + 1, c, INK)                       # closed / half-closed eyes
    else:
        for c in (c1, c2):
            put(e, c, INK)
            put(e + 1, c, INK)
    if mood in ("thriving", "rich"):
        for rc in ((m, 6), (m, 9), (m + 1, 7), (m + 1, 8)):
            put(rc[0], rc[1], INK)                   # smile
        put(f["blush"], c1 - 2, BLUSH)
        put(f["blush"], c2 + 2, BLUSH)
    elif mood == "content":
        put(m + 1, 7, INK)
        put(m + 1, 8, INK)
    elif mood in ("hungry", "exhausted"):
        for rc in ((m + 1, 6), (m, 7), (m + 1, 8), (m, 9)):
            put(rc[0], rc[1], INK)                   # wobbly mouth
        drip = 3 + frame % 4                         # sweat drop sliding down
        put(drip, 13, SWEAT)
        put(drip + 1, 13, SWEAT)
    else:  # asleep
        put(m + 1, 7, INK)
        put(m + 1, 8, INK)
        zy = (frame // 2) % 2
        for r, row in enumerate(Z if mood == "sleep" else QMARK):
            for c, ch in enumerate(row):
                if ch == "#":
                    rr, cc = r + zy, 13 + c
                    if 0 <= rr < H and cc < W:
                        g[rr][cc] = (150, 130, 255, 255) if mood == "sleep" else (90, 160, 250, 255)
    return g


def png(grids, scale=2, gap=3, ground=False):
    """Pack pet grids side by side into a PNG (144 dpi, so 1 pet pixel = `scale`/2 points)."""
    import struct
    import zlib
    H = max(len(gr) for gr in grids) + (1 if ground else 0)
    W = sum(len(gr[0]) for gr in grids) + gap * (len(grids) - 1)
    canvas = [[None] * W for _ in range(H)]
    x = 0
    for gr in grids:
        for r, row in enumerate(gr):
            for c, col in enumerate(row):
                canvas[r][x + c] = col
        x += len(gr[0]) + gap
    if ground:
        for c in range(W):
            if c % 2 == 0:
                canvas[H - 1][c] = (160, 150, 140, 160)
    raw = bytearray()
    for row in canvas:
        line = bytearray()
        for col in row:
            line += bytes(col or (0, 0, 0, 0)) * scale
        for _ in range(scale):
            raw += b"\x00" + line
    Wp, Hp = W * scale, H * scale

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ppm = 5669  # 144 dpi -> crisp on Retina
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", Wp, Hp, 8, 6, 0, 0, 0))
            + chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1)) + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))
    import base64
    return base64.b64encode(data).decode()


_icon_cache = {}


def pet_icon(species, entry):
    mood, life = mood_of(entry)
    k = (species, mood, life)
    if k not in _icon_cache:
        _icon_cache[k] = png([draw_pet(species, mood, life, frame=1)], scale=2)
    return _icon_cache[k]


# ── menu ─────────────────────────────────────────────────────────────────────
TICK = 0.6  # seconds per animation frame


def pet_species(tool):
    return "claude" if tool == "claude" else "codex"


def build_menu(profs, cache, cfg, frame):
    out = []
    emit = out.append
    if profs is None:
        emit("aisw? | sfimage=person.crop.circle.badge.questionmark")
        emit("---")
        emit("aisw isn't installed or isn't on PATH | color=%s" % RED)
        emit("Install aisw | href=https://aiswitcher.dev/cli/")
        return "\n".join(out)

    # menu bar: the active pets, animated, plus their life
    actives = []
    for tool, _, _ in TOOLS:
        act = next((p for p in profs if p["tool"] == tool and p["active"]), None)
        if act:
            actives.append(act)
    grids, bits = [], []
    for p in actives:
        e = cache.get(key_of(p))
        mood, life = mood_of(e)
        grids.append(draw_pet(pet_species(p["tool"]), mood, life, frame))
        bits.append("∞" if mood == "rich" else "…" if life is None else "%d%%" % life)
    if grids:
        emit("%s | image=%s tooltip=Your active accounts' pets — life is the quota left" % ("  ".join(bits), png(grids, 2, 3)))
    else:
        emit("AI pets | sfimage=pawprint")
    emit("---")

    # the habitat: big pets, then one status line each
    if grids:
        emit(" | image=%s" % png(grids, 4, 6, ground=True))
        for p in actives:
            glyph = dict((t, g) for t, _, g in TOOLS)[p["tool"]]
            emit("%s %s  %s | size=12 bash=\"%s\" param1=noop terminal=false" % (
                glyph, safe(p["name"]), mood_text(p, cache.get(key_of(p))), SELF))
        emit("---")

    for tool, tname, glyph in TOOLS:
        mine = [p for p in profs if p["tool"] == tool]
        emit("%s  %s | size=12 color=%s disabled=true" % (glyph, tname, GREY))
        for p in mine:
            account_rows(emit, p, cache.get(key_of(p)) or {})
        emit("＋ Add a %s account… | bash=\"%s\" param1=add param2=%s terminal=false refresh=true size=13 color=%s" % (
            tname, SELF, tool, GREY.split(",")[0]))
        suggest(emit, mine, cache)
        emit("---")

    vs = "Restart" if vscode_running() else "Open"
    emit("%s VS Code with the active accounts | bash=\"%s\" param1=reopen terminal=false refresh=true sfimage=arrow.clockwise" % (vs, SELF))
    emit("Check on everyone now | bash=\"%s\" param1=fetch-now param2=all terminal=false refresh=true sfimage=arrow.triangle.2.circlepath" % SELF)
    emit("Alerts %s — at %d%% life and when a pet falls asleep | bash=\"%s\" param1=toggle param2=alerts terminal=false refresh=true sfimage=%s" % (
        "on" if cfg.get("alerts") else "off", 100 - ALERT_AT, SELF, "bell.badge.fill" if cfg.get("alerts") else "bell.slash"))
    emit("Animation %s | bash=\"%s\" param1=toggle param2=animate terminal=false refresh=true sfimage=%s" % (
        "on" if cfg.get("animate", True) else "off", SELF, "sparkles" if cfg.get("animate", True) else "pause.circle"))
    last = max([e.get("checked_at", 0) for e in cache.values()] or [0])
    emit("Last check %s  ·  hold ⌥ for exact times | size=11 color=%s disabled=true" % (fmt_age(last), GREY))
    return "\n".join(out)


def account_rows(emit, p, e):
    w = worst(e)
    status = e.get("status")
    plan = e.get("plan") or ""
    head = p["name"] + ("  ·  " + plan if plan else "")
    if status == "apikey":
        summary = "pay as you go"
    elif not e:
        summary = "hatching…"
    elif status == "expired":
        summary = "login needs a refresh"
    elif status == "nologin":
        summary = "no login saved"
    elif w and w["pct"] >= 100:
        summary = "asleep · wakes %s" % fmt_reset(w["reset"])
    elif w:
        main = [x for x in e["windows"] if not x.get("scoped")][:2]
        summary = " · ".join("%s %d%%" % (x["label"], left(x["pct"])) for x in main) + " left"
        maxed = [x["label"].rsplit(" ", 1)[0] for x in e["windows"] if x.get("scoped") and x["pct"] >= 100]
        if maxed:
            summary += "  (%s out)" % ", ".join(maxed)
    elif status == "busy":
        summary = "waiting — service busy"
    else:
        summary = "no data yet"
    active = " checked=true" if p["active"] else ""
    emit("%s   —   %s | image=%s bash=\"%s\" param1=noop terminal=false%s" % (
        safe(head), safe(summary), pet_icon(pet_species(p["tool"]), e), SELF, active))

    emit("--%s | size=13" % mood_text(p, e))
    if p["label"]:
        emit("--%s | size=11 color=%s disabled=true" % (safe(p["label"]), GREY))
    for x in e.get("windows") or []:
        line = "%-9s %s %3d%% left   ↻ %s" % (x["label"][:9], bar(x["pct"]), left(x["pct"]), fmt_reset(x["reset"]))
        emit("--%s | font=Menlo size=12 color=%s" % (safe(line), color_for(x["pct"])))
        exact = datetime.datetime.fromtimestamp(x["reset"]).strftime("%a %d %b %H:%M") if x["reset"] else "unknown"
        emit("--%-9s %d%% used · resets %s | font=Menlo size=12 alternate=true" % (safe(x["label"][:9]), round(x["pct"]), exact))
    if e.get("windows"):
        msg = pace(worst(e))
        if msg:
            emit("--%s | size=12" % safe(msg))
    for n in e.get("notes") or []:
        emit("--%s | size=12" % safe(n))
    if e.get("error"):
        emit("--%s | size=12 color=%s" % (safe(e["error"]), AMBER))
    if e.get("blocked_until", 0) > now():
        emit("--Next try %s | size=11 color=%s disabled=true" % (fmt_reset(e["blocked_until"]), GREY))
    if e:
        src = e.get("source") or ""
        emit("--Updated %s%s | size=11 color=%s disabled=true" % (
            fmt_age(e.get("as_of") or e.get("fetched_at")), " · from " + src if src else "", GREY))
    emit("-----")
    if status == "expired":
        emit("--Wake it up — opens %s on this account | bash=\"%s\" param1=wake param2=%s param3=\"%s\" terminal=false refresh=true sfimage=alarm" % (
            dict((t, n) for t, n, _ in TOOLS)[p["tool"]], SELF, p["tool"], safe(p["name"])))
    if p["active"]:
        emit("--✓ This is the active account | disabled=true")
    else:
        emit("--Switch to %s | bash=\"%s\" param1=switch param2=%s param3=\"%s\" terminal=false refresh=true sfimage=arrow.left.arrow.right" % (
            safe(p["name"]), SELF, p["tool"], safe(p["name"])))
    emit("--Check on %s now | bash=\"%s\" param1=fetch-now param2=\"%s\" terminal=false refresh=true sfimage=arrow.clockwise" % (
        safe(p["name"]), SELF, key_of(p)))
    emit("--Remove this account… | bash=\"%s\" param1=remove param2=%s param3=\"%s\" terminal=false refresh=true sfimage=trash color=%s" % (
        SELF, p["tool"], safe(p["name"]), RED.split(",")[0]))


def suggest(emit, mine, cache):
    """If another account has clearly more life than the active one, offer it."""
    act = next((p for p in mine if p["active"]), None)
    if not act:
        return
    aw = worst(cache.get(key_of(act)))
    if not aw or aw["pct"] < 70:
        return
    best, best_pct = None, None
    for p in mine:
        e = cache.get(key_of(p)) or {}
        w = worst(e)
        if p["active"] or e.get("status") != "ok" or not w:
            continue
        if best_pct is None or w["pct"] < best_pct:
            best, best_pct = p, w["pct"]
    if best and best_pct is not None and best_pct + 25 <= aw["pct"]:
        emit("✨ %s is tired — switch to %s (%d%% life) | bash=\"%s\" param1=switch param2=%s param3=\"%s\" terminal=false refresh=true color=%s" % (
            safe(act["name"]), safe(best["name"]), left(best_pct), SELF, best["tool"], safe(best["name"]), GREEN.split(",")[0]))


def vscode_running():
    rc, out, _ = run(["osascript", "-e", 'application id "com.microsoft.VSCode" is running'], timeout=5)
    return out.strip() == "true"


def render_once():
    profs = profiles()
    cache = read_json(CACHE_FILE, {})
    if profs and any(due(p, cache.get(key_of(p)), now()) for p in profs):
        spawn_fetch("due")
    print(build_menu(profs, cache, settings(), int(now() / TICK)))


def stream():
    """SwiftBar streamable mode: redraw every frame so the pets move."""
    frame, loaded_at, mtime = 0, 0, "x"
    profs, cache, cfg = None, {}, settings()
    while True:
        t = now()
        try:
            mt = os.path.getmtime(CACHE_FILE)
        except OSError:
            mt = None
        if t - loaded_at > 30 or mt != mtime:
            profs, cache, cfg, loaded_at, mtime = profiles(), read_json(CACHE_FILE, {}), settings(), t, mt
            if profs and any(due(p, cache.get(key_of(p)), t) for p in profs):
                spawn_fetch("due")
        try:
            sys.stdout.write("~~~\n" + build_menu(profs, cache, cfg, frame) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            return
        frame += 1
        time.sleep(TICK if cfg.get("animate", True) else 15)


# ── click actions ────────────────────────────────────────────────────────────
def aiswitch_bin():
    for c in (os.path.join(HOME, ".local/bin/aiswitch"), "/usr/local/bin/aiswitch", "/opt/homebrew/bin/aiswitch"):
        if os.access(c, os.X_OK):
            return c
    return "aiswitch"


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def refresh_widget():
    run(["open", "-g", "swiftbar://refreshplugin?name=" + urllib.parse.quote(PLUGIN_NAME)], timeout=5)


def dialog(text, buttons, default_button, answer=None, caution=False):
    """macOS dialog. -> (button, typed text) or None if cancelled."""
    script = ['on run a',
              'set btns to {}',
              'repeat with b in items 4 thru -1 of a', 'set end of btns to (b as text)', 'end repeat']
    if answer is None:
        script.append('set r to display dialog (item 1 of a) buttons btns default button (item 2 of a) '
                      'with title "AI pets"%s' % (" with icon caution" if caution else ""))
        script.append('return button returned of r')
    else:
        script.append('set r to display dialog (item 1 of a) default answer (item 3 of a) buttons btns '
                      'default button (item 2 of a) with title "AI pets"')
        script.append('return (button returned of r) & linefeed & (text returned of r)')
    script.append('end run')
    cmd = ["osascript"]
    for line in script:
        cmd += ["-e", line]
    rc, out, _ = run(cmd + [text, default_button, answer or ""] + list(buttons), timeout=600)
    if rc != 0:
        return None  # Cancel pressed
    parts = out.rstrip("\n").split("\n", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def do_switch(tool, name):
    title = dict((t, n) for t, n, _ in TOOLS).get(tool, tool)
    os.chdir(HOME)
    running = vscode_running()
    rc, out, err = run([aiswitch_bin(), tool, name, "--yes" if running else "--no-restart"], timeout=120)
    if rc == 0:
        notify("%s → %s" % (title, name), "Restarting VS Code with this account." if running
               else "Start VS Code with “Open VS Code with the active accounts” in this menu.")
    else:
        lines = [l for l in strip_ansi(out + err).splitlines() if l.strip()]
        notify("Couldn't switch %s" % title, " ".join(lines[-2:]) or "unknown error")


def do_reopen():
    os.chdir(HOME)
    rc, out, err = run([aiswitch_bin(), "--reopen", "--yes"], timeout=120)
    notify("VS Code", "Reopened with the active accounts." if rc == 0 else
           " ".join(strip_ansi(out + err).split()[-20:]))


def do_add(tool):
    tname = dict((t, n) for t, n, _ in TOOLS)[tool]
    login = "Sign in with ChatGPT" if tool == "codex" else "Sign in with Claude"
    how = dialog("Add a new %s account.\n\nSigning in opens your browser. With an API key, you'll paste the key in "
                 "a Terminal window (it never passes through this widget)." % tname,
                 ["Cancel", "API key", login], login)
    if not how:
        return
    existing = set(p["name"] for p in (profiles() or []) if p["tool"] == tool)
    prompt, name = "Give it a short name (e.g. work, personal, client-acme):", ""
    for _ in range(4):
        got = dialog(prompt, ["Cancel", "Add"], "Add", answer=name)
        if not got:
            return
        name = got[1].strip()
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$", name):
            prompt = "Use letters, numbers, dots, dashes or underscores (no spaces). Try another name:"
        elif name in existing:
            prompt = "There's already a %s account called “%s”. Pick another name:" % (tname, name)
        else:
            break
    else:
        return
    if how[0] == "API key":
        body = ('read -r -s -p "Paste the API key (hidden) and press Return: " KEY; echo\n'
                'printf "%%s" "$KEY" | aisw add %s %s --api-key-stdin\nstatus=$?\nunset KEY\n' % (tool, name))
    else:
        body = 'aisw add %s %s\nstatus=$?\n' % (tool, name)
    script = """#!/bin/bash
export PATH="%(path)s"
clear
echo "🥚  Adding the %(tname)s account “%(name)s”"
echo
%(body)s
open -g "swiftbar://refreshplugin?name=%(plugin)s"
"%(py)s" "%(self)s" fetch "%(tool)s:%(name)s" >/dev/null 2>&1 &
echo
if [ $status -eq 0 ]; then echo "🐣  Done! Your new pet will hatch in the menu bar."; else echo "❌  That didn't work (see above). Nothing was changed."; fi
echo
read -n1 -s -r -p "Press any key to close this window…"
rm -f "$0"
""" % {"path": os.environ["PATH"], "tname": tname, "name": name, "body": body, "plugin": PLUGIN_NAME,
       "py": sys.executable, "self": SELF, "tool": tool}
    open_in_terminal("add-%s-%s.command" % (tool, name), script)


def open_in_terminal(fname, script):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, fname)
    with open(path, "w") as f:
        f.write(script)
    os.chmod(path, 0o700)
    run(["open", path], timeout=10)


def do_wake(tool, name):
    """Open Terminal running Claude Code / Codex on this account. It renews its own login as it
    starts (or asks you to sign in); aipets itself still never touches the login or sends a prompt."""
    pdir = os.path.join(AISW_HOME, "profiles", tool, name)
    if not (re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name) and os.path.isdir(pdir)):
        return
    tname = dict((t, n) for t, n, _ in TOOLS)[tool]
    script = """#!/bin/bash
export PATH="%(path)s"
export %(var)s="%(dir)s"
trap ':' INT  # a Ctrl-C quits the tool, not this script
clear
echo "⏰  Waking up the %(tname)s account “%(name)s”"
echo
echo "%(tname)s renews its login as it starts. If it asks you to sign in, do that."
echo "Then quit it (type %(quit)s) and the pet wakes up."
echo
%(tool)s
echo
echo "🐣  Checking on it…"
"%(py)s" "%(self)s" fetch "%(tool)s:%(name)s" >/dev/null 2>&1
open -g "swiftbar://refreshplugin?name=%(plugin)s"
read -n1 -s -r -p "Done. Press any key to close this window…"
rm -f "$0"
""" % {"path": os.environ["PATH"], "var": "CLAUDE_CONFIG_DIR" if tool == "claude" else "CODEX_HOME", "dir": pdir,
       "quit": "/exit" if tool == "claude" else "/quit", "tname": tname, "name": name, "tool": tool,
       "py": sys.executable, "self": SELF, "plugin": PLUGIN_NAME}
    open_in_terminal("wake-%s-%s.command" % (tool, name), script)


def do_remove(tool, name):
    tname = dict((t, n) for t, n, _ in TOOLS)[tool]
    service = "Anthropic" if tool == "claude" else "OpenAI"
    active = any(p["tool"] == tool and p["name"] == name and p["active"] for p in profiles() or [])
    note = ("It's the active account, so %s will have no account selected until you add or switch to "
            "another one.\n\n" % tname) if active else ""
    ok = dialog("Remove the %s account “%s”?\n\n%sThis deletes the login saved for it on this Mac (aisw keeps a backup "
                "you can restore). Your %s account itself isn't affected." % (tname, name, note, service),
                ["Cancel", "Remove"], "Cancel", caution=True)
    if not ok or ok[0] != "Remove":
        return
    cmd = ["aisw", "remove", tool, name, "--yes", "--non-interactive"] + (["--force"] if active else [])
    rc, out, err = run(cmd, timeout=60)
    if rc == 0:
        cache = read_json(CACHE_FILE, {})
        cache.pop("%s:%s" % (tool, name), None)
        write_json(CACHE_FILE, cache)
        notify("👋 Said goodbye to %s" % name, "The %s account was removed from this Mac." % tname)
    else:
        msg = " ".join(l for l in strip_ansi(out + err).splitlines() if l.strip())
        notify("Couldn't remove %s" % name, msg[-200:] or "unknown error")
    refresh_widget()


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "fetch":
        target = argv[2] if len(argv) > 2 else "due"
        fetch("all" if target == "all" else ([] if target == "due" else [target]))
    elif cmd == "fetch-now":
        spawn_fetch(argv[2] if len(argv) > 2 else "all")
    elif cmd == "switch":
        do_switch(argv[2], argv[3])
    elif cmd == "reopen":
        do_reopen()
    elif cmd == "add":
        do_add(argv[2])
    elif cmd == "remove":
        do_remove(argv[2], argv[3])
    elif cmd == "wake":
        do_wake(argv[2], argv[3])
    elif cmd == "toggle":
        if len(argv) > 2 and argv[2] in ("alerts", "animate"):
            s = settings()
            s[argv[2]] = not s.get(argv[2], True)
            write_json(SETTINGS_FILE, s)
    elif cmd == "noop":
        pass
    elif cmd == "once" or not os.environ.get("SWIFTBAR"):
        render_once()
    else:
        stream()


if __name__ == "__main__":
    main(sys.argv)
