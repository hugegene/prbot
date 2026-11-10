#!/usr/bin/env python3
"""prbot: watch GitHub for PRs that need you and open a Claude tab in zellij for each.

  rev-N    your review was requested                 -> "Review PR #N."
  rerev-N  re-requested, or the author replied to you -> check your comments are resolved
  fix-N    reviewers commented on your own PR        -> address them, in that branch's worktree
  re-N     you were @mentioned on someone else's PR  -> draft a reply

Claude starts in your own clone (REPO_DIRS), so it uses the repo's CLAUDE.md and skills.
Posting or pushing (gh pr review/comment, gh api, git push) always needs your OK in the tab.

Commands:
  poll [--dry-run] [--once] [--since ISO]   poller loop (bin/start runs it in a tab)
  new-job KIND N|URL [--repo OWNER/REPO]     open a tab by hand (KIND: review rereview fix reply)
  ls                                         prbot tabs and the queue
  threads N [--repo OWNER/REPO]              print a PR's reviews and comment threads
  cleanup [--dry-run]                        remove worktrees (and local branches) whose PR has merged
  run KIND REPO N [EXTRA...]                 internal: runs inside a tab
  hook EVENT                                 internal: Claude Code hook
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
LOG_FILE = os.path.join(HERE, "prbot.log")
CACHE = os.path.expanduser("~/.cache/prbot")
API = "https://api.github.com"
KINDS = {"review": "rev", "rereview": "rerev", "fix": "fix", "reply": "re"}
PRIORITY = ["review", "rereview", "fix", "reply"]

MESSAGES = {
    "review": "Review PR #{n}. Skip reading in /review. Wait for me before posting anything to GitHub.",
    "rereview": "{intro} Pull the PR and check whether the author's replies and any new commits resolve my review "
                "comments (my last review was on commit {sha}; threads: {prbot}/bin/pr-threads {n}). "
                "Wait for me before posting anything to GitHub.",
    "fix": "Address the reviewers' comments on my PR #{n}. You're in its branch's worktree "
           "(threads: {prbot}/bin/pr-threads {n}).{note} Wait for me before pushing or posting anything to GitHub.",
    "reply": "I was mentioned on PR #{n}: {url} (threads: {prbot}/bin/pr-threads {n}). "
             "Draft my reply and wait for me before posting it.",
}
DONE_CLAUSE = (' When I say "close the tab", run {prbot}/bin/prbot done after everything else I asked for '
               "in that message (such as posting) has succeeded.")


# ---------------------------------------------------------------- config / log

def load_config():
    cfg = {"GH_USER": "", "POLL_SECS": "60", "MAX_JOBS": "3", "LAG_SECS": "300", "ZELLIJ_SESSION": "prbot",
           "CLAUDE_MODEL": "", "NTFY_TOPIC": "", "IGNORE_AUTHORS": "Copilot", "PERMISSION_MODE": "auto",
           "REPO_DIRS": "", "CLOSE_GRACE_SECS": "15", "CLAUDE_EFFORT": ""}
    path = os.path.join(HERE, "config.env")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


CFG = load_config()
SESSION = CFG["ZELLIJ_SESSION"]


def log(msg):
    line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def die(msg):
    print(f"prbot: {msg}", file=sys.stderr)
    sys.exit(1)


def repo_dirs():
    out = {}
    for pair in CFG["REPO_DIRS"].split(","):
        if "=" in pair:
            name, path = (s.strip() for s in pair.split("=", 1))
            out[name.lower()] = os.path.expanduser(path)
    return out


def repo_dir(repo):
    path = repo_dirs().get(repo.lower())
    return path if path and os.path.isdir(os.path.join(path, ".git")) else None


def default_repo():
    names = list(repo_dirs())
    return names[0] if len(names) == 1 else None


# ---------------------------------------------------------------- time

def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s):
    if not s:
        return None
    if len(s) == 10:
        s += "T00:00:00Z"
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


# ---------------------------------------------------------------- GitHub

_token = None


def token():
    global _token
    if _token is None:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            die("could not get a token from `gh auth token` - run `gh auth login`")
        _token = r.stdout.strip()
    return _token


class GHError(Exception):
    def __init__(self, status, body, url):
        super().__init__(f"HTTP {status} for {url}: {body[:500]}")
        self.status = status


def gh_request(method, path, body=None, params=None):
    url = path if path.startswith("http") else API + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(6):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token()}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "prbot",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", errors="replace")
                return (json.loads(raw) if raw else None), r.headers
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            limited = e.code in (403, 429) and (e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Remaining") == "0")
            if limited and attempt < 5:
                wait = int(e.headers.get("Retry-After") or int(e.headers.get("X-RateLimit-Reset", time.time() + 60)) - time.time())
                wait = max(5, min(wait, 900))
                log(f"rate limited; waiting {wait}s")
                time.sleep(wait)
                continue
            if e.code >= 500 and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            raise GHError(e.code, text, url)
        except (urllib.error.URLError, TimeoutError):
            if attempt < 5:
                time.sleep(2 ** attempt)
                continue
            raise


def gh_get(path, **params):
    return gh_request("GET", path, params=params or None)[0]


def gh_get_all(path, **params):
    params.setdefault("per_page", 100)
    out, url, first = [], path, True
    while url:
        data, headers = gh_request("GET", url, params=params if first else None)
        first = False
        out.extend(data)
        m = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", "") or "")
        url = m.group(1) if m else None
    return out


def search_prs(q):
    return gh_get("/search/issues", q=q, sort="updated", order="desc", per_page=100)["items"]


def repo_of(item):
    return item["repository_url"].split("/repos/", 1)[1]


def me():
    if not CFG["GH_USER"]:
        CFG["GH_USER"] = gh_get("/user")["login"]
    return CFG["GH_USER"]


def from_human(user):
    login = user["login"].lower()
    ignored = {a.strip().lower() for a in CFG["IGNORE_AUTHORS"].split(",") if a.strip()}
    return login != me().lower() and login not in ignored and user.get("type") != "Bot" and not login.endswith("[bot]")


def mentions_me(text):
    unquoted = "\n".join(l for l in (text or "").splitlines() if not l.lstrip().startswith(">"))
    return bool(re.search(r"(?<![\w-])@" + re.escape(me()) + r"(?![\w-])", unquoted, re.I))


REVIEWED_AT_RE = re.compile(r"Reviewed at:\s*`([0-9a-f]{7,40})`", re.I)  # the PR template's review line


def my_latest_review(repo, n):
    """Your latest review: a submitted GitHub review, or any comment you posted in the PR conversation (reviews
    often go up as plain comments). Returns {"submitted_at": newest of those, "commit_id": the newest one's commit
    that's known (a review's commit, or a comment's `Reviewed at:` line), else the PR's head at that time}."""
    mine = me().lower()
    items = [(r["submitted_at"], r["commit_id"]) for r in gh_get_all(f"/repos/{repo}/pulls/{n}/reviews")
             if r["user"]["login"].lower() == mine and r.get("submitted_at")]
    for c in gh_get_all(f"/repos/{repo}/issues/{n}/comments"):
        if c["user"]["login"].lower() == mine:
            m = REVIEWED_AT_RE.search(c.get("body") or "")
            items.append((c["created_at"], m.group(1) if m else None))
    if not items:
        return None
    items.sort(key=lambda i: parse_ts(i[0]))
    when = items[-1][0]
    commit = next((sha for _, sha in reversed(items) if sha), None)
    if not commit or len(commit) < 40:
        commits = gh_get_all(f"/repos/{repo}/pulls/{n}/commits")
        if commit:  # a short sha from a comment: use the full one when it's in the PR
            commit = next((c["sha"] for c in commits if c["sha"].startswith(commit.lower())), commit)
        elif commits:  # no commit given: the newest PR commit made before the comment (dates can lie after a rebase)
            before = [c for c in commits if parse_ts(c["commit"]["committer"]["date"]) <= parse_ts(when)]
            commit = (before or commits)[-1]["sha"]
    return {"submitted_at": when, "commit_id": commit or ""}


# ---------------------------------------------------------------- discovery

def task(kind, repo, n, title, keys, extra=(), trigger_at=None):
    """A job. rereview's extra is [your last review's commit, reason, links...]; reason is request, reply,
    request+reply or manual. trigger_at is when the newest request or comment behind the job happened."""
    return {"kind": kind, "repo": repo, "number": n, "title": title, "keys": list(keys), "extra": list(extra),
            "trigger_at": trigger_at}


def merge_tasks(x, t):
    """Two jobs for the same PR become one: the higher-priority kind wins, and what triggered both is kept."""
    keep = min(x, t, key=lambda y: PRIORITY.index(y["kind"]))
    m = dict(keep, title=t["title"], keys=sorted(set(x["keys"]) | set(t["keys"])), notified=x.get("notified", False),
             trigger_at=max(x.get("trigger_at") or "", t.get("trigger_at") or "") or None)
    if x["kind"] == t["kind"] == "fix":
        m["extra"] = (x["extra"] + t["extra"])[-10:]
    elif x["kind"] == t["kind"] == "rereview":
        reason = lambda e: set((e[1] if len(e) > 1 else "request").split("+"))
        m["extra"] = [t["extra"][0], "+".join(sorted(reason(x["extra"]) | reason(t["extra"]))),
                      *(x["extra"][2:] + t["extra"][2:])[-10:]]
    elif x["kind"] == t["kind"]:
        m["extra"] = t["extra"]
    return m


def latest_request_event(repo, n):
    """The newest "review requested" event on the PR that's for you (or, failing that, for a team)."""
    evs = [e for e in gh_get_all(f"/repos/{repo}/issues/{n}/timeline") if e.get("event") == "review_requested"]
    mine = [e for e in evs if ((e.get("requested_reviewer") or {}).get("login") or "").lower() == me().lower()]
    pick = mine or [e for e in evs if e.get("requested_team")]
    return max(pick, key=lambda e: e["created_at"]) if pick else None


def author_replies(repo, n, author, after):
    """The PR author's comments posted after `after`: in the conversation, or replies in inline threads you're in."""
    out = []
    rcs = gh_get_all(f"/repos/{repo}/pulls/{n}/comments")
    root = lambda c: c.get("in_reply_to_id") or c["id"]
    my_threads = {root(c) for c in rcs if c["user"]["login"].lower() == me().lower()}
    for c in rcs:
        if c["user"]["login"].lower() == author and root(c) in my_threads and parse_ts(c["created_at"]) > after:
            out.append((f"review_comment:{c['id']}", c["html_url"], c["created_at"]))
    for c in gh_get_all(f"/repos/{repo}/issues/{n}/comments", since=iso(after)):
        if c["user"]["login"].lower() == author and parse_ts(c["created_at"]) > after:
            out.append((f"issue_comment:{c['id']}", c["html_url"], c["created_at"]))
    return sorted(out, key=lambda a: a[2])


def i_commented_after(repo, n, t):
    """True if you posted any comment on the PR after time t."""
    for path in (f"/repos/{repo}/issues/{n}/comments", f"/repos/{repo}/pulls/{n}/comments"):
        if any(c["user"]["login"].lower() == me().lower() and parse_ts(c["created_at"]) > t
               for c in gh_get_all(path, since=iso(t))):
            return True
    return False


def new_activity(repo, n, since, only_mentions):
    """Comments and reviews on PR n since `since`, from other people. Returns [(key, url, created_at)]."""
    out = []
    s = iso(since)
    for c in gh_get_all(f"/repos/{repo}/issues/{n}/comments", since=s):
        if from_human(c["user"]) and (not only_mentions or mentions_me(c.get("body"))):
            out.append((f"issue_comment:{c['id']}", c["html_url"], c["created_at"]))
    for c in gh_get_all(f"/repos/{repo}/pulls/{n}/comments", since=s):
        if from_human(c["user"]) and (not only_mentions or mentions_me(c.get("body"))):
            out.append((f"review_comment:{c['id']}", c["html_url"], c["created_at"]))
    for r in gh_get_all(f"/repos/{repo}/pulls/{n}/reviews"):
        ts = parse_ts(r.get("submitted_at"))
        if ts and ts >= since and from_human(r["user"]) and r["state"] != "PENDING":
            if only_mentions and not mentions_me(r.get("body")):
                continue
            if not only_mentions and r["state"] == "APPROVED" and not (r.get("body") or "").strip():
                continue  # a bare approval needs nothing from you
            out.append((f"review:{r['id']}", r["html_url"], r["submitted_at"]))
    return out


def discover(state):
    since = parse_ts(state["last_check"]) - dt.timedelta(seconds=int(CFG["LAG_SECS"]))
    since_q = since.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    seen = state.setdefault("seen", {})
    base = "is:pr is:open archived:false"
    found = {}  # (repo, n) -> task; one task per PR, highest priority wins

    def add(t):
        k = (t["repo"], t["number"])
        found[k] = merge_tasks(found[k], t) if k in found else t

    # 1) review requested: each request event fires once. Never reviewed -> rev; reviewed before -> rerev
    for it in search_prs(f"{base} review-requested:@me"):
        repo, n = repo_of(it), it["number"]
        ev = latest_request_event(repo, n)
        key = f"req_event:{ev['id']}" if ev else f"req_pending:{repo}#{n}"
        if key in seen:
            continue
        if any(k.startswith(f"req:{repo}#{n}@") for k in seen):  # handled under the old commit-based key
            seen[key] = iso(now())
            continue
        when = ev["created_at"] if ev else iso(now())
        mine = my_latest_review(repo, n)
        if mine:
            add(task("rereview", repo, n, it["title"], [key], [mine["commit_id"], "request"], when))
        else:
            add(task("review", repo, n, it["title"], [key], [], when))

    # 2) the PR author answered your review: new comments after your latest review, in the conversation
    #    or in inline threads you're part of. Pushes alone don't trigger anything. A review can also be a plain
    #    comment you posted, so PRs you've only commented on are searched too.
    reviewed = {}
    for q in (f"{base} reviewed-by:@me -author:@me updated:>={since_q}", f"{base} commenter:@me -author:@me updated:>={since_q}"):
        for it in search_prs(q):
            reviewed[(repo_of(it), it["number"])] = it
    for (repo, n), it in reviewed.items():
        mine = my_latest_review(repo, n)
        if not mine:
            continue
        after = max(parse_ts(mine["submitted_at"]), since)
        new = [a for a in author_replies(repo, n, it["user"]["login"].lower(), after) if a[0] not in seen]
        if new:
            add(task("rereview", repo, n, it["title"], [a[0] for a in new],
                     [mine["commit_id"], "reply", *[a[1] for a in new][-10:]], new[-1][2]))

    # 3) reviewers commented on your own PR
    for it in search_prs(f"{base} author:@me updated:>={since_q}"):
        repo, n = repo_of(it), it["number"]
        new = [a for a in new_activity(repo, n, since, only_mentions=False) if a[0] not in seen]
        if new:
            new.sort(key=lambda a: a[2])
            add(task("fix", repo, n, it["title"], [a[0] for a in new], [a[1] for a in new][-10:]))

    # 4) @mentions on someone else's PR
    cands = {}
    for q in (f"{base} mentions:@me -author:@me updated:>={since_q}", f"{base} involves:@me -author:@me updated:>={since_q}"):
        for it in search_prs(q):
            cands[(repo_of(it), it["number"])] = it
    for (repo, n), it in cands.items():
        new = [a for a in new_activity(repo, n, since, only_mentions=True) if a[0] not in seen]
        if parse_ts(it["created_at"]) >= since and from_human(it["user"]) and mentions_me(it.get("body")) \
                and f"pr_body:{it['id']}" not in seen:
            new.append((f"pr_body:{it['id']}", it["html_url"], it["created_at"]))
        if new:
            new.sort(key=lambda a: a[2])
            add(task("reply", repo, n, it["title"], [a[0] for a in new], [new[-1][1]]))
    return list(found.values())


# ---------------------------------------------------------------- zellij

def zj(*args, session=None):
    return subprocess.run(["zellij", "-s", session or SESSION, "action", *args], capture_output=True, text=True)


def session_running():
    r = subprocess.run(["zellij", "list-sessions", "-n"], capture_output=True, text=True)
    return any(l.split()[0] == SESSION and "EXITED" not in l for l in r.stdout.splitlines() if l.split())


def ensure_session():
    if session_running():
        return
    subprocess.run(["zellij", "delete-session", SESSION], capture_output=True)
    subprocess.run(["zellij", "attach", "--create-background", SESSION], capture_output=True, env=clean_env())
    for _ in range(20):
        if session_running():
            return
        time.sleep(0.25)
    raise RuntimeError(f"could not start zellij session {SESSION}")


def list_tabs():
    if not session_running():
        return []
    r = zj("list-tabs", "-j", "-s")
    try:
        return json.loads(r.stdout) if r.returncode == 0 else []
    except ValueError:
        return []


TAB_RE = re.compile(r"^(?:[*✓] )?(rev|rerev|fix|re)-(\d+)$")  # "* " = draft ready, "✓ " = closing


def prbot_tabs():
    return [(t, TAB_RE.match(t["name"])) for t in list_tabs() if TAB_RE.match(t["name"])]


def tab_name(t):
    return f"{KINDS[t['kind']]}-{t['number']}"


def find_tab(name):
    return next((t for t, m in prbot_tabs() if re.sub(r"^[*✓] ", "", t["name"]) == name), None)


def own_tab_id():
    """The tab this process runs in, found through its own pane; None outside the prbot session.
    Never look a tab up by name here: another tab can have the same name."""
    pane = os.environ.get("ZELLIJ_PANE_ID")
    if not pane or os.environ.get("ZELLIJ_SESSION_NAME") != SESSION:
        return None
    try:
        panes = json.loads(zj("list-panes", "-j", "-t").stdout)
    except ValueError:
        return None
    return next((p["tab_id"] for p in panes if not p.get("is_plugin") and str(p.get("id")) == pane), None)


def spawn(t):
    ensure_session()
    focused = next((x["tab_id"] for x in list_tabs() if x.get("active")), None)
    r = zj("new-tab", "-n", tab_name(t), "-c", repo_dir(t["repo"]), "--",
           os.path.join(HERE, "bin", "run-claude"), t["kind"], t["repo"], str(t["number"]), *t["extra"])
    if r.returncode != 0:
        raise RuntimeError(f"zellij new-tab failed: {r.stderr.strip()}")
    if focused is not None:  # don't pull you out of the tab you're in
        zj("go-to-tab-by-id", str(focused))


def notify(title, body=""):
    try:
        subprocess.run(["notify-send", "-a", "prbot", title, body], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if CFG["NTFY_TOPIC"]:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://ntfy.sh/{CFG['NTFY_TOPIC']}", data=body.encode(),
                headers={"Title": title.encode("ascii", "replace").decode()}), timeout=10).read()
        except Exception:
            pass


WHAT = {"review": "Review requested", "rereview": "Re-review (re-requested, or the author replied)",
        "fix": "New comments on your PR", "reply": "You were mentioned"}


def enqueue(state, t):
    """Add a job; if one for the same PR is already waiting, merge them (higher-priority kind wins)."""
    q = state.setdefault("queue", [])
    for i, x in enumerate(q):
        if (x["repo"], x["number"]) == (t["repo"], t["number"]):
            q[i] = merge_tasks(x, t)
            return
    q.append(t)


def revalidate(t):
    """Re-check a waiting job against the PR as it is now. Returns the job to open, or None to drop it."""
    repo, n = t["repo"], t["number"]
    pr = gh_get(f"/repos/{repo}/pulls/{n}")
    if pr["state"] != "open" and t["kind"] != "reply":
        return None
    if t["kind"] not in ("review", "rereview"):
        return t
    mine = my_latest_review(repo, n)
    trig = parse_ts(t.get("trigger_at")) if t.get("trigger_at") else None
    if trig is None:  # a job queued before this version: the old commit-based rule
        if mine and mine.get("commit_id") == pr["head"]["sha"]:
            return None
        return dict(t, kind="rereview", extra=[mine["commit_id"], "request"]) if mine else t
    if mine and parse_ts(mine["submitted_at"]) > trig:
        return None  # you've reviewed since the request or the author's reply
    if t["kind"] == "review":
        return dict(t, kind="rereview", extra=[mine["commit_id"], "request"]) if mine else t
    reason = t["extra"][1] if len(t["extra"]) > 1 else "request"
    if "request" not in reason and i_commented_after(repo, n, trig):
        return None  # you've already answered the author
    return dict(t, extra=[mine["commit_id"] if mine else t["extra"][0], *t["extra"][1:]])


def launch_pending(state):
    queue = state.setdefault("queue", [])
    tabs = prbot_tabs()
    open_prs = {int(m.group(2)) for _, m in tabs}
    count = len(tabs)
    keep = []
    for t in queue:
        label = f"{t['repo']}#{t['number']}"
        if not repo_dir(t["repo"]):
            log(f"{label}: skipped, no clone for {t['repo']} in REPO_DIRS")
            continue
        if t["number"] in open_prs:  # hold it until that tab closes
            t["waiting"] = "its tab is open"
            if not t.get("notified"):
                t["notified"] = True
                log(f"{label}: new {t['kind']} activity; waiting for its open tab to close")
                notify(f"prbot: more activity on {label}", "it opens when the current tab closes")
            keep.append(t)
            continue
        if count >= int(CFG["MAX_JOBS"]):
            t["waiting"] = "MAX_JOBS reached"
            keep.append(t)
            continue
        try:
            ready = revalidate(t)
        except Exception as e:
            log(f"{label}: could not re-check ({e}); will retry")
            keep.append(t)
            continue
        if ready is None:
            log(f"{label}: nothing left to do for {t['kind']}; dropped")
            continue
        try:
            spawn(ready)
            count += 1
            open_prs.add(ready["number"])
            log(f"opened tab {tab_name(ready)}: {ready['title']}")
            notify(f"prbot: {WHAT[ready['kind']]} - {label}", ready["title"])
        except Exception as e:
            log(f"{label}: could not open tab ({e}); will retry")
            keep.append(t)
    state["queue"] = keep


# ---------------------------------------------------------------- Claude settings

# Variables a Claude session sets for the commands it runs. If they reach the zellij session (prbot started from
# inside Claude), every pane inherits them, and a `claude` started there thinks it's a child session: it doesn't
# save its transcript and points at the old session's socket. bin/start unsets the same list.
LEAKY_ENV = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_BRIDGE_SESSION_ID",
             "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
             "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SSE_PORT", "CLAUDE_PID", "CLAUDE_EFFORT")
def clean_env():
    return {k: v for k, v in os.environ.items() if k not in LEAKY_ENV}


SAFE_MODES = ("auto", "manual", "default", "acceptEdits", "plan")  # never bypassPermissions / dontAsk


def settings_file():
    """Settings layered on your normal ones: prbot's hooks, plus read-only commands allowed up front. No ask or
    deny rules (your choice, 2026-11-02): the permission mode decides everything else, and posting waits only
    on the first message's "Wait for me before posting"."""
    hook = lambda ev: [{"hooks": [{"type": "command", "command": f"python3 {HERE}/prbot.py hook {ev}"}]}]
    s = {
        "permissions": {
            "allow": [f"Bash({c}:*)" for c in ("gh pr view", "gh pr diff", "gh pr checks", "gh pr list",
                                               "gh issue view", f"{HERE}/bin/pr-threads", f"{HERE}/bin/prbot done")],
        },
        "hooks": {"Stop": hook("stop"), "Notification": hook("notification"), "UserPromptSubmit": hook("prompt")},
    }
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "claude-settings.json")
    with open(path, "w") as f:
        json.dump(s, f, indent=2)
    return path


# ---------------------------------------------------------------- fix-N worktrees

WORKTREES_FILE = os.path.join(HERE, "worktrees.json")  # worktrees used by fix tabs, for cleanup after merge


class PrbotError(Exception):
    pass


def git(cwd, *args, check=True):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise PrbotError(f"git {' '.join(args)} failed: {r.stderr.strip()[:400]}")
    return r.stdout.strip()


def has_local_branch(clone, branch):
    return subprocess.run(["git", "-C", clone, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0


def list_worktrees(clone):
    out, cur = [], {}
    for line in git(clone, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur:
                out.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            cur["branch"] = line[len("branch refs/heads/"):]
        elif line.startswith("locked"):
            cur["locked"] = True
    return out


def default_branch(clone):
    ref = git(clone, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    return ref.split("/", 1)[1] if "/" in ref else "main"


def same_path(a, b):
    return os.path.realpath(a) == os.path.realpath(b)


@contextlib.contextmanager
def worktree_records():
    """worktrees.json, read-modify-write under a lock: the poller and the tabs both write it."""
    with open(WORKTREES_FILE + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recs = {}
        if os.path.exists(WORKTREES_FILE):
            with open(WORKTREES_FILE) as f:
                recs = json.load(f)
        yield recs
        tmp = WORKTREES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(recs, f, indent=2)
        os.replace(tmp, WORKTREES_FILE)


def prepare_fix_worktree(repo, n):
    """Find or create the worktree for PR n's source branch and bring it up to date when that's safe.
    Returns (path, note for Claude)."""
    clone = repo_dir(repo)
    pr = gh_get(f"/repos/{repo}/pulls/{n}")
    branch = pr["head"]["ref"]
    head_repo = ((pr["head"].get("repo") or {}).get("full_name") or "").lower()
    if branch in {"main", "master", default_branch(clone)}:
        raise PrbotError(f"PR #{n} comes from {branch!r}; refusing to work on the default branch")
    if head_repo != repo.lower():
        raise PrbotError(f"PR #{n} comes from a fork ({head_repo or 'deleted repo'}); fix tabs only support same-repo branches")
    git(clone, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    existing = next((w for w in list_worktrees(clone) if w.get("branch") == branch), None)
    if existing:
        path = existing["path"]
    else:
        path = os.path.join(clone, ".claude", "worktrees", re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-"))
        if os.path.exists(path):
            raise PrbotError(f"{path} already exists but isn't the worktree for {branch}")
        if has_local_branch(clone, branch):
            git(clone, "worktree", "add", "--quiet", path, branch)
        else:
            git(clone, "worktree", "add", "--quiet", "--track", "-b", branch, path, f"origin/{branch}")
        log(f"created worktree {path} for {branch} (PR #{n})")
    note = ""
    ahead, behind = map(int, git(path, "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}").split())
    if git(path, "status", "--porcelain"):
        note = " Note: this worktree has uncommitted changes I made; don't commit them unless I say."
    elif ahead and behind:
        note = f" Note: the branch has diverged from origin/{branch} ({ahead} local commits, {behind} new on GitHub)."
    elif behind:
        git(path, "merge", "--ff-only", "--quiet", f"origin/{branch}")
    elif ahead:
        note = f" Note: the branch has {ahead} local commit(s) that aren't pushed yet."
    with worktree_records() as recs:
        old = recs.get(f"{repo}:{branch}", {})
        recs[f"{repo}:{branch}"] = {"repo": repo, "branch": branch, "pr": n, "path": path,
                                    "created_by_prbot": old.get("created_by_prbot", existing is None),
                                    "since": old.get("since", iso(now()))}
    return path, note


def processes_inside(path):
    """True if any process (a shell, an editor, a Claude session) has its working folder inside path."""
    path = os.path.realpath(path)
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue
        if cwd == path or cwd.startswith(path + os.sep):
            return True
    return False


def cleanup_decision(repo, branch, path):
    """Whether a branch's worktree can go. Returns (action, pr, reason); action is remove, keep or wait."""
    clone = repo_dir(repo)
    prs = gh_get(f"/repos/{repo}/pulls", head=f"{repo.split('/')[0]}:{branch}", state="all", per_page=20)
    if any(p["state"] == "open" for p in prs):
        return "wait", None, "a PR from this branch is still open"
    merged = sorted((p for p in prs if p.get("merged_at")), key=lambda p: p["merged_at"])
    if not merged:
        return "keep", (prs[0] if prs else None), ("its PR was closed without merging" if prs else "no PR for this branch")
    pr = merged[-1]
    if same_path(path, clone):
        return "keep", pr, "it's your main clone"
    if not os.path.isdir(path):
        return "remove", pr, ""
    wt = next((w for w in list_worktrees(clone) if same_path(w["path"], path)), {})
    if wt.get("locked"):
        return "keep", pr, "the worktree is locked (git worktree unlock it to allow removal)"
    tip = git(clone, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if tip and tip != pr["head"]["sha"]:
        return "keep", pr, f"the branch has commits after what was merged ({tip[:7]}, merged {pr['head']['sha'][:7]})"
    if git(path, "status", "--porcelain"):
        return "keep", pr, "it has uncommitted changes or untracked files"
    if any(int(m.group(2)) == pr["number"] for _, m in prbot_tabs()):
        return "keep", pr, "a prbot tab for it is open"
    if processes_inside(path):
        return "keep", pr, "something is running inside it (a shell or Claude session?)"
    return "remove", pr, ""


def remove_worktree(repo, branch, path):
    clone = repo_dir(repo)
    if os.path.isdir(path):
        git(clone, "worktree", "remove", path)  # no --force: git refuses if anything is left
    git(clone, "worktree", "prune")
    if has_local_branch(clone, branch):
        git(clone, "branch", "-D", branch)  # -D: squash merges aren't "merged" to git; the tip check made it safe


def auto_cleanup():
    """Each poll: remove worktrees that fix tabs used, once their PR has merged and nothing would be lost."""
    if not os.path.exists(WORKTREES_FILE):
        return
    with worktree_records() as recs:
        for key, rec in list(recs.items()):
            try:
                action, pr, why = cleanup_decision(rec["repo"], rec["branch"], rec["path"])
                if action == "remove":
                    remove_worktree(rec["repo"], rec["branch"], rec["path"])
                    del recs[key]
                    log(f"removed worktree {rec['path']} and branch {rec['branch']} (PR #{pr['number']} merged)")
                    notify(f"prbot: cleaned up {rec['branch']}", f"PR #{pr['number']} merged; worktree and local branch removed")
                elif action == "keep" and rec.get("warned") != why:
                    rec["warned"] = why
                    log(f"kept worktree {rec['path']}: {why}")
                    notify(f"prbot: kept worktree for {rec['branch']}", why)
            except Exception as e:
                log(f"cleanup of {rec.get('path')} failed: {e}")


# ---------------------------------------------------------------- commands

def cmd_poll(a):
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    if a.since:
        state["last_check"] = iso(parse_ts(a.since))
    state.setdefault("last_check", iso(now()))
    log(f"poller started as @{me()}; activity after {state['last_check']} counts; every {CFG['POLL_SECS']}s"
        + (" [dry-run]" if a.dry_run else ""))
    while True:
        try:
            start = now()
            for t in discover(state):
                if a.dry_run:
                    log(f"[dry-run] would open {tab_name(t)} ({t['kind']}): {t['repo']}#{t['number']} {t['title']}")
                    continue
                for k in t["keys"]:
                    state["seen"][k] = iso(now())
                enqueue(state, t)
                log(f"new {t['kind']} job: {t['repo']}#{t['number']} {t['title']}")
            if not a.dry_run:
                launch_pending(state)
                auto_cleanup()
            state["last_check"] = iso(start)
            cutoff = now() - dt.timedelta(days=30)
            state["seen"] = {k: v for k, v in state.get("seen", {}).items() if parse_ts(v) > cutoff}
            if not a.dry_run:
                tmp = STATE_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(state, f, indent=2)
                os.replace(tmp, STATE_FILE)
        except Exception:
            log("poll error:\n" + traceback.format_exc())
        if a.once:
            return
        time.sleep(int(CFG["POLL_SECS"]))


COMMENT_URL_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


def cmd_new_job(a):
    if a.kind == "reply":
        m = COMMENT_URL_RE.search(a.target)
        if not m:
            die("usage: new-job reply <comment URL>")
        repo, n, extra = m.group(1), int(m.group(2)), [a.target]
    else:
        repo, n, extra = a.repo or default_repo(), int(a.target), []
        if not repo:
            die("pass --repo OWNER/REPO")
        if a.kind == "rereview":
            mine = my_latest_review(repo, n)
            if not mine:
                die(f"you haven't reviewed #{n} yet - use: new-job review {n}")
            pr = gh_get(f"/repos/{repo}/pulls/{n}")
            replies = author_replies(repo, n, pr["user"]["login"].lower(), parse_ts(mine["submitted_at"]))
            extra = [mine["commit_id"], "manual", *[r[1] for r in replies][-10:]]
    if not repo_dir(repo):
        die(f"no clone for {repo} in REPO_DIRS (config.env)")
    pr = gh_get(f"/repos/{repo}/pulls/{n}")
    t = task(a.kind, repo, n, pr["title"], [], extra)
    if find_tab(tab_name(t)):
        die(f"tab {tab_name(t)} is already open")
    spawn(t)
    print(f"opened tab {tab_name(t)} in zellij session '{SESSION}'. Attach with: zellij attach {SESSION}")


def cmd_ls(a):
    tabs = prbot_tabs()
    print("open tabs:" if tabs else "no prbot tabs open")
    for t, _ in tabs:
        print(f"  {t['name']}")
    if os.path.exists(STATE_FILE):
        q = json.load(open(STATE_FILE)).get("queue", [])
        if q:
            print("waiting:")
            for t in q:
                print(f"  {tab_name(t):<10} {t['repo']}#{t['number']:<6} ({t.get('waiting', 'next poll')}) {t['title'][:50]}")


THREADS_Q = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
 title url author{login} headRefName headRefOid
 reviews(last:30){nodes{databaseId author{login} state body submittedAt commit{oid}}}
 comments(last:60){nodes{databaseId author{login} body createdAt}}
 reviewThreads(first:100){nodes{isResolved isOutdated path line originalLine
  comments(first:30){nodes{databaseId author{login} body createdAt}}}}}}}"""


def cmd_threads(a):
    repo = a.repo or default_repo()
    if not repo:
        die("pass --repo OWNER/REPO")
    o, r = repo.split("/")
    data = gh_request("POST", "/graphql", body={"query": THREADS_Q, "variables": {"o": o, "r": r, "n": int(a.number)}})[0]
    if data.get("errors"):
        die(json.dumps(data["errors"])[:500])
    pr = data["data"]["repository"]["pullRequest"]
    who = lambda x: (x.get("author") or {}).get("login", "ghost")
    print(f"# PR #{a.number}: {pr['title']}\n{pr['url']} · @{who(pr)} · branch {pr['headRefName']} @ {pr['headRefOid'][:7]}\n")
    print("## Reviews")
    for rv in pr["reviews"]["nodes"]:
        print(f"- @{who(rv)} {rv['state']} at {rv['submittedAt']} on {(rv.get('commit') or {}).get('oid', '')[:7]} (id {rv['databaseId']})")
        if (rv.get("body") or "").strip():
            print("  " + rv["body"].strip().replace("\n", "\n  "))
    print("\n## Code threads (reply with: gh api repos/%s/pulls/%s/comments/<first comment id>/replies -f body=...)" % (repo, a.number))
    for th in pr["reviewThreads"]["nodes"]:
        status = "resolved" if th["isResolved"] else "OPEN"
        status += ", outdated" if th["isOutdated"] else ""
        print(f"\n### [{status}] {th['path']}:{th.get('line') or th.get('originalLine')}")
        for c in th["comments"]["nodes"]:
            print(f"- @{who(c)} at {c['createdAt']} (id {c['databaseId']}):\n  " + (c.get("body") or "").strip().replace("\n", "\n  "))
    print("\n## Conversation")
    for c in pr["comments"]["nodes"]:
        print(f"- @{who(c)} at {c['createdAt']} (id {c['databaseId']}):\n  " + (c.get("body") or "").strip().replace("\n", "\n  "))


def rereview_intro(n, extra):
    """First sentence of a rerev message: why the tab opened."""
    reason = extra[1] if len(extra) > 1 else "manual"
    links = " ".join(u for u in extra[2:] if u.startswith("https://github.com/"))
    if "request" in reason and links:
        return f"My review was re-requested on PR #{n}, and the author replied: {links}."
    if "request" in reason:
        return f"My review was re-requested on PR #{n}."
    if links:
        return f"The author replied to my review on PR #{n}: {links}."
    return f"Re-check my review on PR #{n}."


def cmd_run(a):
    """Inside a zellij tab: start Claude in your clone with a one-line instruction, then close the tab."""
    def pause(msg):
        try:
            input(f"{msg} - press Enter to close this tab. ")
        except EOFError:
            pass
    if a.kind not in KINDS or not re.match(r"^[\w.-]+/[\w.-]+$", a.repo) or not a.number.isdigit():
        return pause("prbot: bad arguments")
    cwd = repo_dir(a.repo)
    if not cwd:
        return pause(f"prbot: no clone for {a.repo} in REPO_DIRS")
    n = int(a.number)
    name = f"{KINDS[a.kind]}-{n}"
    note = ""
    if a.kind == "fix":  # your own PR: work in its branch's worktree (found or created)
        try:
            cwd, note = prepare_fix_worktree(a.repo, n)
        except (PrbotError, GHError) as e:
            return pause(f"prbot: {e}")
    msg = (MESSAGES[a.kind] + DONE_CLAUSE).format(n=n, prbot=HERE, sha=(a.extra[0][:12] if a.extra else ""),
                                                  url=(a.extra[0] if a.extra else ""), note=note,
                                                  intro=rereview_intro(n, a.extra))
    try:
        if not os.get_terminal_size().columns:
            raise OSError
    except OSError:  # a tab opened while nobody is attached can start at 0x0
        subprocess.run(["stty", "cols", "160", "rows", "48"], capture_output=True)
    mode = CFG["PERMISSION_MODE"] if CFG["PERMISSION_MODE"] in SAFE_MODES else "manual"
    args = ["claude", "--settings", settings_file(), "--permission-mode", mode]
    if CFG["CLAUDE_MODEL"]:
        args += ["--model", CFG["CLAUDE_MODEL"]]
    if CFG["CLAUDE_EFFORT"] in ("low", "medium", "high", "xhigh", "max"):
        args += ["--effort", CFG["CLAUDE_EFFORT"]]  # this session only; your default elsewhere is unchanged
    args += ["-n", name, msg]  # -n last: options that take lists would swallow the message
    env = clean_env()
    env["PRBOT_TAB"] = name
    rc = subprocess.run(args, cwd=cwd, env=env).returncode
    if rc != 0:
        pause(f"claude exited with code {rc}")
    tab = own_tab_id()
    if tab is not None:
        zj("close-tab-by-id", str(tab))


CLOSING = os.path.join(CACHE, "closing")


def closing_mark():
    return os.path.join(CLOSING, re.sub(r"\D", "", os.environ.get("ZELLIJ_PANE_ID", "")) or "none")


def cmd_done(a):
    """Run by Claude once you've decided: mark the tab, then close it after a grace period."""
    tab, name = own_tab_id(), os.environ.get("PRBOT_TAB", "")
    if tab is None or not TAB_RE.match(name):
        die("not inside a prbot tab, so there's nothing to close")
    os.makedirs(CLOSING, exist_ok=True)
    mark = closing_mark()
    open(mark, "w").close()
    zj("rename-tab-by-id", str(tab), "✓ " + name)
    secs = max(0, int(CFG["CLOSE_GRACE_SECS"]))
    # The session goes along explicitly: the closer must act on the session this tab is in, nothing else.
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "close-later", SESSION, str(tab), mark, name, str(secs)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
    print(f"Closing this tab in {secs}s. Type anything to keep it open.")


def cmd_close_later(a):
    time.sleep(int(a.secs))
    if os.path.exists(a.mark):
        os.remove(a.mark)
        zj("close-tab-by-id", a.tab, session=a.session)
    else:  # you typed something during the grace period
        zj("rename-tab-by-id", a.tab, a.name, session=a.session)


def cmd_cleanup(a):
    """By hand: every worktree in your clones whose branch's PR has merged (not only ones fix tabs used)."""
    for repo, clone in repo_dirs().items():
        if not repo_dir(repo):
            continue
        default = default_branch(clone)
        for w in list_worktrees(clone):
            branch = w.get("branch")
            if not branch or same_path(w["path"], clone) or branch in ("main", "master", default):
                continue
            label = f"{w['path']}  [{branch}]"
            action, pr, why = cleanup_decision(repo, branch, w["path"])
            prn = f"PR #{pr['number']}" if pr else "no PR"
            if action != "remove":
                print(f"keep     {label}: {why}" + (f" ({prn})" if pr and action == "keep" else ""))
            elif a.dry_run:
                print(f"remove   {label}: {prn} merged  (dry run: nothing done)")
            else:
                remove_worktree(repo, branch, w["path"])
                with worktree_records() as recs:
                    recs.pop(f"{repo}:{branch}", None)
                print(f"removed  {label}: {prn} merged; worktree and local branch deleted")
                log(f"cleanup: removed {w['path']} and branch {branch} ({prn} merged)")


def cmd_hook(a):
    name = os.environ.get("PRBOT_TAB", "")
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    if not TAB_RE.match(name):
        return
    if a.event == "prompt":  # you sent a message: cancel a pending close (prints nothing: output becomes context)
        try:
            os.remove(closing_mark())
        except OSError:
            pass
        return
    if a.event == "notification":
        notify(f"prbot: {name} needs you", payload.get("message", ""))
        return
    if os.path.exists(closing_mark()):
        return  # closing: keep the ✓ name
    # stop: the first time Claude finishes in this session, mark the tab and tell you
    marks = os.path.join(CACHE, "notified")
    os.makedirs(marks, exist_ok=True)
    sid = re.sub(r"[^\w-]", "", str(payload.get("session_id", name)))
    mark = os.path.join(marks, sid)
    if os.path.exists(mark):
        return
    open(mark, "w").close()
    for f in os.listdir(marks):
        p = os.path.join(marks, f)
        if time.time() - os.path.getmtime(p) > 7 * 86400:
            os.remove(p)
    tab = own_tab_id()  # hooks run inside Claude, which runs inside the tab's pane
    if tab is not None:
        zj("rename-tab-by-id", str(tab), "* " + name)
    notify(f"prbot: {name} is ready for you")


def main():
    p = argparse.ArgumentParser(prog="prbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("poll"); s.set_defaults(fn=cmd_poll)
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--once", action="store_true"); s.add_argument("--since")
    s = sub.add_parser("new-job"); s.set_defaults(fn=cmd_new_job)
    s.add_argument("kind", choices=list(KINDS)); s.add_argument("target"); s.add_argument("--repo")
    s = sub.add_parser("ls"); s.set_defaults(fn=cmd_ls)
    s = sub.add_parser("threads"); s.set_defaults(fn=cmd_threads); s.add_argument("number"); s.add_argument("--repo")
    s = sub.add_parser("cleanup"); s.set_defaults(fn=cmd_cleanup); s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("run"); s.set_defaults(fn=cmd_run)
    s.add_argument("kind"); s.add_argument("repo"); s.add_argument("number"); s.add_argument("extra", nargs="*")
    s = sub.add_parser("hook"); s.set_defaults(fn=cmd_hook); s.add_argument("event", choices=["stop", "notification", "prompt"])
    s = sub.add_parser("done"); s.set_defaults(fn=cmd_done)
    s = sub.add_parser("close-later"); s.set_defaults(fn=cmd_close_later)
    for arg in ("session", "tab", "mark", "name", "secs"):
        s.add_argument(arg)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
