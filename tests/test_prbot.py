"""Tests for prbot. Run them from the repo folder:

    python3 -m unittest discover -s tests -v

They work on a copy of prbot in a temporary folder, with its own config.env, so they never touch yours, your
state or your sessions: tos and zellij are pointed at throwaway socket folders and a throwaway HOME for the whole
run. The multiplexer tests start throwaway sessions there, and are skipped when that program isn't installed.
Nothing reaches GitHub, and a stub stands in for Claude.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The variables a tos or zellij pane has. MUX=auto and SESSION=auto read them, and these tests may themselves run
# inside a pane, so they're cleared for the whole run. A test that wants them sets them.
PANE_VARS = ("TOS_PANE_ID", "TOS_SESSION", "ZELLIJ_PANE_ID", "ZELLIJ_SESSION_NAME", "ZELLIJ")
_saved = {}


def setUpModule():
    """Clears the pane variables, and points tos and zellij at throwaway folders, so no test can see or change your
    sessions. (prbot looks for a running "prbot" session in both.) Socket paths must stay short, so they go
    straight under /tmp."""
    home = tempfile.mkdtemp()
    isolated = {"HOME": home, "TOS_STATE_DIR": os.path.join(home, "tos-state"),
                "TOS_SOCKET_DIR": f"/tmp/prbt-{os.getpid()}-t", "ZELLIJ_SOCKET_DIR": f"/tmp/prbt-{os.getpid()}-z"}
    for k in (*PANE_VARS, *isolated):
        _saved[k] = os.environ.get(k)
        os.environ.pop(k, None)
    os.environ.update(isolated)


def tearDownModule():
    for k in ("HOME", "TOS_SOCKET_DIR", "ZELLIJ_SOCKET_DIR"):
        shutil.rmtree(os.environ[k] if k != "HOME" else os.environ["HOME"], True)
    for k, v in _saved.items():
        restore_env(k, v)


def pane_of(name, session="outer"):
    """The variables a pane of `name` (tos or zellij) has, in `session`."""
    return {"tos": {"TOS_PANE_ID": "3", "TOS_SESSION": session},
            "zellij": {"ZELLIJ_PANE_ID": "3", "ZELLIJ_SESSION_NAME": session, "ZELLIJ": "0"}}[name]


def make_copy(tmp, config):
    """A copy of prbot in tmp/prbot, with `config` as its config.env. Returns its folder."""
    root = os.path.join(tmp, "prbot")
    shutil.copytree(os.path.join(REPO, "bin"), os.path.join(root, "bin"))
    shutil.copy(os.path.join(REPO, "prbot.py"), root)
    write_config(root, config)
    return root


def write_config(root, config):
    with open(os.path.join(root, "config.env"), "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in config.items()))


def load(root):
    """The copy's prbot.py as a new module. It reads its config.env as it's imported."""
    spec = importlib.util.spec_from_file_location(f"prbot_{len(sys.modules)}", os.path.join(root, "prbot.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_for(check, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    return check()


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def module(self, **config):
        return load(make_copy(self.tmp, config))

    def test_defaults(self):
        m = self.module()
        self.assertEqual((m.MUX_SETTING, m.SESSION_SETTING), ("auto", "auto"))

    def test_zellij_and_the_old_session_setting(self):
        m = self.module(MUX="zellij", ZELLIJ_SESSION="old")
        self.assertEqual((m.MUX, m.SESSION, m.SESSION_VAR), ("zellij", "old", "ZELLIJ_SESSION_NAME"))
        shutil.rmtree(os.path.join(self.tmp, "prbot"))
        self.assertEqual(self.module(MUX="zellij", SESSION="new", ZELLIJ_SESSION="old").SESSION, "new")

    def test_an_unknown_mux_stops_with_a_message(self):
        root = make_copy(self.tmp, {"MUX": "screen"})
        r = subprocess.run([sys.executable, os.path.join(root, "prbot.py"), "ls"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("MUX=screen in config.env; use auto, tos or zellij", r.stderr)

    def test_one_pair_of_quotes_is_removed(self):
        m = self.module(MESSAGE_REVIEW='"Review #{n} "carefully""', CLAUDE_MODEL="it's")
        self.assertEqual(m.CFG["MESSAGE_REVIEW"], 'Review #{n} "carefully"')
        self.assertEqual(m.CFG["CLAUDE_MODEL"], "it's")


class Messages(unittest.TestCase):
    FIELDS = dict(n=487, prbot="/p", sha="abc", url="https://x", note="", intro="Hi.")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def module(self, **config):
        return load(make_copy(self.tmp, config))

    def test_the_default_message_and_the_close_sentence(self):
        m = self.module()
        msg = m.first_message("review", self.FIELDS)
        self.assertTrue(msg.startswith("Review PR #487. "), msg)
        self.assertIn(f'When I say "close the tab", run {m.HERE}/bin/prbot done', msg)

    def test_config_replaces_a_message_but_not_the_close_sentence(self):
        m = self.module(MESSAGE_REVIEW="Look at #{n} with {{care}}.")
        msg = m.first_message("review", self.FIELDS)
        self.assertTrue(msg.startswith("Look at #487 with {care}. When I say \"close the tab\""), msg)
        self.assertTrue(m.first_message("fix", self.FIELDS).startswith("Address the reviewers' comments"))

    def test_the_example_config_shows_each_default_message(self):
        m = self.module()
        with open(os.path.join(REPO, "config.example.env")) as f:
            example = f.read()
        for kind, msg in m.MESSAGES.items():
            self.assertIn(f"# {msg}\nMESSAGE_{kind.upper()}=\n", example, kind)

    def test_an_unknown_placeholder_is_explained(self):
        m = self.module(MESSAGE_FIX="Fix {number}")
        with self.assertRaises(m.PrbotError) as e:
            m.first_message("fix", self.FIELDS)
        self.assertIn("MESSAGE_FIX in config.env", str(e.exception))
        self.assertIn("{n}", str(e.exception))

    def test_an_unclosed_brace_is_explained(self):
        m = self.module(MESSAGE_FIX="Fix {n")
        with self.assertRaises(m.PrbotError):
            m.first_message("fix", self.FIELDS)


class Notifications(unittest.TestCase):
    def test_macos_uses_osascript_with_the_texts_as_arguments(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        m = load(make_copy(tmp, {}))
        platform = sys.platform
        try:
            sys.platform = "darwin"
            cmd = m.desktop_notification('a "quoted" title', "body")
        finally:
            sys.platform = platform
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[-2:], ['a "quoted" title', "body"])
        self.assertNotIn("quoted", " ".join(cmd[1:-2]))
        self.assertEqual(m.desktop_notification("t", "b")[:1], ["notify-send"] if platform != "darwin" else ["osascript"])


class ProcessCheck(unittest.TestCase):
    """The check before cleanup removes a worktree: is anything running inside it?"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.m = load(make_copy(self.tmp, {}))
        self.inside = os.path.join(self.tmp, "worktree", "sub")
        self.outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(self.inside)
        os.makedirs(self.outside)
        self.proc = subprocess.Popen(["sleep", "60"], cwd=self.inside)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.kill)

    def check(self, method):
        self.assertIs(self.m.processes_inside(os.path.join(self.tmp, "worktree"), method), True)
        self.assertIs(self.m.processes_inside(self.outside, method), False)

    @unittest.skipUnless(os.path.isdir("/proc/self"), "no /proc")
    def test_proc(self):
        self.check("proc")

    @unittest.skipUnless(shutil.which("lsof"), "lsof isn't installed")
    def test_lsof(self):
        self.check("lsof")

    def test_no_way_to_tell(self):
        self.assertIsNone(self.m.processes_inside(self.outside, "none"))


class TosVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.bin)
        path = os.environ["PATH"]
        os.environ["PATH"] = self.bin + os.pathsep + path
        self.addCleanup(os.environ.__setitem__, "PATH", path)
        self.m = load(make_copy(self.tmp, {"MUX": "tos"}))

    def fake_tos(self, version):
        with open(os.path.join(self.bin, "tos"), "w") as f:
            f.write(f"#!/bin/sh\necho '{version}'\n")
        os.chmod(os.path.join(self.bin, "tos"), 0o755)

    def test_api_2_is_enough(self):
        self.fake_tos("tos 0.3.1 (extension API 2)")
        self.assertIsNone(self.m.mux_problem())

    def test_older_tos_says_to_update(self):
        self.fake_tos("tb 0.1.1 (extension API 1)")
        self.assertIn("Run: tos update", self.m.mux_problem())


class Where(unittest.TestCase):
    """MUX=auto and SESSION=auto: where prbot works, and how it says it chose. tos and zellij are stand-ins that
    write down their arguments, and list a running "prbot" session when told to."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def module(self, installed=("tos", "zellij"), running=(), panes=(), started=None, **config):
        self.bin = tempfile.mkdtemp(dir=self.tmp)
        for name in installed:
            listed = "printf 'prbot [1 tabs, 0 attached]\\n'" if name in running else "true"
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(f'#!/bin/sh\necho "$@" >> "{self.bin}/{name}.log"\n'
                        f'if [ "$1" = list-sessions ]; then {listed}; fi\nexit 0\n')
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.root = make_copy(tempfile.mkdtemp(dir=self.tmp), dict({"MUX": "auto", "SESSION": "auto"}, **config))
        if started:
            with open(os.path.join(self.root, "started"), "w") as f:
                f.write(started)
        self.env = {"PATH": self.bin}
        for name in panes:
            self.env.update(pane_of(name))
        with env_set(self.env):
            return load(self.root)

    def where(self, m):
        return m.MUX, m.SESSION, m.OWNED, m.WHY

    def test_your_pane_gives_the_multiplexer_and_the_session(self):
        for name in ("tos", "zellij"):
            self.assertEqual(self.where(self.module(panes=[name])), (name, "outer", False, "you're in it"))

    def test_outside_any_session_prbots_own_where_it_runs(self):
        self.assertEqual(self.where(self.module(running=["zellij"])),
                         ("zellij", "prbot", True, "outside any session, so prbot's own; its session is running in zellij"))

    def test_outside_with_none_running_the_only_one_installed(self):
        expected = ("tos", "prbot", True, "outside any session, so prbot's own; the only multiplexer installed")
        self.assertEqual(self.where(self.module(installed=["tos"])), expected)
        # Inside both (one started inside the other) can't be told apart, so it counts as outside.
        self.assertEqual(self.where(self.module(installed=["tos"], panes=["tos", "zellij"])), expected)

    def test_when_it_cant_tell_it_says_so(self):
        m = self.module()  # both installed, no prbot session running, outside any session
        self.assertIsNone(m.MUX)
        r = subprocess.run([sys.executable, os.path.join(self.root, "prbot.py"), "ls"], capture_output=True,
                           text=True, env=dict(os.environ, **self.env))
        self.assertEqual(r.returncode, 1)
        self.assertIn("can't tell whether to use tos or zellij", r.stderr)

    def test_where_start_put_it_beats_the_pane(self):
        m = self.module(panes=["tos"], started="zellij\nmain\n")
        self.assertEqual(self.where(m), ("zellij", "main", False, "where ./bin/start put it"))
        # A file from before sessions were remembered: prbot's own session.
        m = self.module(started="zellij\n")
        self.assertEqual(self.where(m), ("zellij", "prbot", True, "where ./bin/start put it"))

    def test_settings_always_win(self):
        m = self.module(MUX="tos", SESSION="work", panes=["zellij"], started="zellij\nmain\n")
        self.assertEqual(self.where(m), ("tos", "work", True, "MUX=tos in config.env; SESSION=work in config.env"))
        m = self.module(SESSION="prbot", panes=["zellij"])
        self.assertEqual(self.where(m), ("zellij", "prbot", True, "you're in a zellij pane; SESSION=prbot in config.env"))

    def test_your_session_is_never_started_or_deleted(self):
        m = self.module(started="tos\nmain\n")  # main isn't running: the stand-in lists nothing
        with env_set(self.env):
            with self.assertRaises(RuntimeError) as e:
                m.ensure_session()
        self.assertIn("prbot doesn't start or delete your own sessions", str(e.exception))
        with open(os.path.join(self.bin, "tos.log")) as f:
            self.assertEqual(set(f.read().split()), {"list-sessions", "-n"})


def env_set(values):
    """Sets these environment variables for a `with` block."""
    import contextlib

    @contextlib.contextmanager
    def block():
        saved = {k: os.environ.get(k) for k in values}
        os.environ.update(values)
        try:
            yield
        finally:
            for k, v in saved.items():
                restore_env(k, v)
    return block()


class Check(unittest.TestCase):
    """./bin/prbot check, with stand-ins for gh, tos, claude and the notification program."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.bin)
        self.sent = os.path.join(self.tmp, "notified")
        notifier = "osascript" if sys.platform == "darwin" else "notify-send"
        for name, body in (("gh", "echo fake-token"), ("tos", "echo 'tos 0.3.1 (extension API 2)'"),
                           ("claude", "true"), (notifier, f'echo "$@" > "{self.sent}"')):
            self.fake(name, body)
        clone = os.path.join(self.tmp, "widgets")
        os.makedirs(os.path.join(clone, ".git"))
        self.root = make_copy(self.tmp, {"MUX": "tos", "GH_USER": "tester",
                                         "REPO_DIRS": f"acme/widgets={clone},acme/gone=/nowhere"})

    def fake(self, name, body):
        with open(os.path.join(self.bin, name), "w") as f:
            f.write(f"#!/bin/sh\n{body}\n")
        os.chmod(os.path.join(self.bin, name), 0o755)

    def check(self):
        env = dict(os.environ, PATH=self.bin + os.pathsep + "/usr/bin:/bin")
        return subprocess.run([sys.executable, os.path.join(self.root, "prbot.py"), "check"], capture_output=True,
                              text=True, env=env)

    def test_it_says_what_works_and_what_doesnt(self):
        r = self.check()
        self.assertEqual(r.returncode, 1, r.stdout)  # acme/gone has no clone
        out = r.stdout
        self.assertIn("ok  multiplexer: tos, session 'prbot' (tos 0.3.1 (extension API 2))", out)
        self.assertIn("ok  gh: logged in; watching for @tester", out)
        self.assertIn("ok  REPO_DIRS acme/widgets:", out)
        self.assertIn("NO  REPO_DIRS acme/gone: /nowhere isn't a git clone", out)
        self.assertIn("ok  notifications: sent one with", out)
        with open(self.sent) as f:
            self.assertIn("prbot: test notification", f.read())

    def test_an_old_tos_and_no_claude(self):
        self.fake("tos", "echo 'tb 0.1.1 (extension API 1)'")
        os.remove(os.path.join(self.bin, "claude"))
        out = self.check().stdout
        self.assertIn("NO  multiplexer: prbot needs tos with extension API 2 or later", out)
        if not shutil.which("claude", path="/usr/bin:/bin"):  # a system-wide claude would be found anyway
            self.assertIn("NO  claude: not installed", out)


class MuxSession:
    """Rounds on one real multiplexer, as prbot uses it. A subclass sets MUX."""

    MUX = ""

    def setUp(self):
        if not shutil.which(self.MUX):
            self.skipTest(f"{self.MUX} isn't installed")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.session = f"prbot-test-{os.getpid()}"
        self.sessions = [self.session]  # every session a test may start, ended afterwards
        # Its own folders for both multiplexers, inside the run's throwaway ones.
        self.sockets = [f"/tmp/prbt-{os.getpid()}-{self.MUX}-t", f"/tmp/prbt-{os.getpid()}-{self.MUX}-z"]
        env = {"HOME": os.path.join(self.tmp, "home"), "TOS_SOCKET_DIR": self.sockets[0],
               "ZELLIJ_SOCKET_DIR": self.sockets[1], "TOS_STATE_DIR": os.path.join(self.tmp, "tos-state")}
        os.makedirs(env["HOME"])
        for d in self.sockets:
            self.addCleanup(shutil.rmtree, d, True)
        # GitHub is out of reach: `gh auth token` gives a fake token, and the API's address is a closed port, so
        # the poller keeps retrying instead of exiting.
        fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(fakebin)
        with open(os.path.join(fakebin, "gh"), "w") as f:
            f.write("#!/bin/sh\necho fake-token\n")
        os.chmod(os.path.join(fakebin, "gh"), 0o755)
        env["PATH"] = fakebin + os.pathsep + os.environ["PATH"]
        for k, v in env.items():
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(restore_env, k, old)
        clone = os.path.join(self.tmp, "widgets")
        subprocess.run(["git", "init", "-q", clone], check=True)
        self.config = {"MUX": self.MUX, "SESSION": self.session, "GH_USER": "tester",
                       "REPO_DIRS": f"acme/widgets={clone}", "CLOSE_GRACE_SECS": "1", "POLL_SECS": "3600"}
        self.root = make_copy(self.tmp, self.config)
        with open(os.path.join(self.root, "prbot.py")) as f:
            src = f.read()
        with open(os.path.join(self.root, "prbot.py"), "w") as f:
            f.write(src.replace('API = "https://api.github.com"', 'API = "http://127.0.0.1:9"'))
        # Claude's stand-in writes down the variables the tab's pane has, then waits to be closed.
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)
        with open(os.path.join(self.root, "bin", "run-claude"), "w") as f:
            f.write(f'#!/bin/sh\nenv > "{self.out}/run-$3.env"\nexec sleep 300\n')
        self.addCleanup(self.end_sessions)
        self.m = load(self.root)

    def run_mux(self, *args):
        return self.m.run_mux(self.MUX, *args)

    def end(self, session, forget=True):
        if self.MUX == "tos":
            self.run_mux("kill", session)  # it doesn't ask (only kill-all-sessions takes -y)
            if forget:
                self.run_mux("delete-session", "-f", session)
        else:
            self.run_mux("kill-session", session)
            if forget:
                self.run_mux("delete-session", "--force", session)

    def end_sessions(self):
        """Ends every session the test may have started, then checks that no session server is left running."""
        for session in self.sessions:
            self.end(session)
        if not os.path.isdir("/proc/self"):
            return
        left = wait_for(lambda: not self.servers(), 5) or self.servers()
        if left is not True:
            for pid in left:
                os.kill(pid, 15)
            self.fail(f"session servers left running: {left}")

    def servers(self):
        """The pids of session servers using this test's socket folders, found through /proc."""
        found = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read()
                with open(f"/proc/{pid}/environ", "rb") as f:
                    env = f.read()
            except OSError:
                continue
            if (b"__server" in cmd or b"--server" in cmd) and any(d.encode() in cmd + env for d in self.sockets):
                found.append(int(pid))
        return found

    def prbot(self, *args, env=None):
        r = subprocess.run([sys.executable, os.path.join(self.root, "prbot.py"), *args], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def names(self, session=None):
        r = self.run_mux("-s", session or self.session, "action", "list-tabs", "-j")
        return [t["name"] for t in json.loads(r.stdout)] if r.returncode == 0 and r.stdout.strip() else []

    def listed(self):
        """The names of every session listed, running or exited."""
        return [line.split()[0] for line in self.run_mux("list-sessions", "-n").stdout.splitlines() if line.split()]

    def test_a_round(self):
        """start, a job's tab, its pane finding its own tab, "close the tab", and stop."""
        m = self.m
        # 1. start: a session whose first tab is "home", and the poller's tab. It's typed in a pane of the other
        #    multiplexer, whose variables must not reach the new session's tabs.
        other = "zellij" if self.MUX == "tos" else "tos"
        out = self.prbot("start", env=dict(os.environ, **pane_of(other)))
        self.assertIn(f"poller started in {self.MUX} session '{self.session}'", out)
        if os.path.isdir("/proc/self"):
            pid = wait_for(lambda: poller_pid(self.root))
            with open(f"/proc/{pid}/environ", "rb") as f:
                poller_env = dict(v.decode().split("=", 1) for v in f.read().split(b"\0") if b"=" in v)
            self.assertEqual([k for k in pane_of(other) if k in poller_env], [])
        self.assertEqual(self.names()[0], "home")
        self.assertIn("poller", self.names())
        self.assertIn("poller is already running", self.prbot("start"))

        # 2. A review job opens rev-487 in the background: you stay on the tab you were on. (With nobody attached,
        #    zellij calls no tab active, so there it's None before and after.)
        active = lambda: next((t["name"] for t in m.list_tabs() if t.get("active")), None)
        before = active()
        m.spawn(m.task("review", "acme/widgets", 487, "A title", []))
        self.assertIsNotNone(m.find_tab("rev-487"))
        self.assertEqual(active(), before)
        env_file = os.path.join(self.out, "run-487.env")
        self.assertTrue(wait_for(lambda: os.path.exists(env_file) and os.path.getsize(env_file)), "the tab never ran")

        # 3. Inside that tab, prbot finds its own tab through its pane's variables.
        with open(env_file) as f:
            pane_env = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
        self.assertEqual(pane_env.get(m.SESSION_VAR), self.session)
        tab = m.find_tab("rev-487")["tab_id"]
        with env_set({m.PANE_VAR: pane_env[m.PANE_VAR], m.SESSION_VAR: pane_env[m.SESSION_VAR]}):
            self.assertEqual(m.own_tab_id(), tab)

        # 4. "close the tab": done marks it ✓, and it closes after CLOSE_GRACE_SECS.
        done_env = dict(os.environ, PRBOT_TAB="rev-487", **{k: pane_env[k] for k in (m.PANE_VAR, m.SESSION_VAR)})
        self.assertIn("Closing this tab in 1s", self.prbot("done", env=done_env))
        self.assertIn("✓ rev-487", self.names())
        self.assertTrue(wait_for(lambda: not any("rev-487" in n for n in self.names())), self.names())

        # 5. stop closes the poller's tab, and says so when there's none.
        r = subprocess.run([os.path.join(self.root, "bin", "stop")], capture_output=True, text=True)
        self.assertIn("poller stopped", r.stdout, r.stderr)
        self.assertNotIn("poller", self.names())
        self.assertIn("poller not running", self.prbot("stop"))

    def test_mux_auto_uses_the_one_you_start_it_in(self):
        write_config(self.root, dict(self.config, MUX="auto"))
        other = "zellij" if self.MUX == "tos" else "tos"
        here, there = dict(os.environ, **pane_of(self.MUX)), dict(os.environ, **pane_of(other))
        # 1. Typed in a pane of this multiplexer: it uses this one, says why, and remembers where.
        out = self.prbot("start", env=here)
        self.assertIn(f"poller started in {self.MUX} session '{self.session}' "
                      f"(you're in a {self.MUX} pane; SESSION={self.session} in config.env)", out)
        with open(os.path.join(self.root, "started")) as f:
            self.assertEqual(f.read().split(), [self.MUX, self.session])
        # 2. Typed in the other one, while the poller runs: it says where, and starts nothing.
        out = self.prbot("start", env=there)
        self.assertIn(f"poller is already running in {self.MUX} session '{self.session}'. "
                      f"To move it to {other} session '{self.session}'", out)
        self.assertEqual(self.names().count("poller"), 1)
        # 3. Every other command works where start put it, wherever it's typed.
        self.assertIn("poller stopped", self.prbot("stop", env=there))
        self.assertNotIn("poller", self.names())

    def test_session_auto_joins_the_session_you_start_it_in(self):
        user = f"user-{os.getpid()}"
        self.sessions.append(user)
        self.run_mux("attach", "--create-background", user)
        first = wait_for(lambda: self.names(user)[:1])
        self.assertTrue(first, "the test's own session didn't start")
        write_config(self.root, dict(self.config, MUX="auto", SESSION="auto"))
        # 1. Typed in a tab of that session: the poller joins it. Your first tab keeps its name, and no session of
        #    prbot's own is made.
        out = self.prbot("start", env=dict(os.environ, **pane_of(self.MUX, user)))
        self.assertIn(f"poller started in {self.MUX} session '{user}' (you're in it)", out)
        self.assertNotIn("attach:", out)
        self.assertEqual(self.names(user)[:1], first)
        self.assertIn("poller", self.names(user))
        self.assertNotIn("prbot", self.listed())
        # 2. stop, typed outside any session, finds it there, and doesn't suggest ending your session.
        out = self.prbot("stop")
        self.assertIn(f"poller stopped. Open Claude tabs keep running in your {self.MUX} session '{user}'.", out)
        self.assertNotIn("kill-session", out)
        # 3. Your session ends. prbot neither starts it again nor deletes what's saved of it.
        self.end(user, forget=False)
        self.assertTrue(wait_for(lambda: user not in self.m.running_sessions(self.MUX)))
        listed = self.listed()
        later = load(self.root)
        self.assertEqual((later.MUX, later.SESSION, later.OWNED), (self.MUX, user, False))
        with self.assertRaises(RuntimeError) as e:
            later.ensure_session()
        self.assertIn("prbot doesn't start or delete your own sessions", str(e.exception))
        self.assertEqual(self.listed(), listed)

    def test_session_auto_outside_any_session_uses_prbots_own(self):
        self.sessions.append("prbot")
        write_config(self.root, dict(self.config, SESSION="auto"))
        # Typed in a plain terminal: prbot's own session, started in the background, with its first tab "home".
        out = self.prbot("start")
        self.assertIn(f"poller started in {self.MUX} session 'prbot' "
                      f"(outside any session, so prbot's own; MUX={self.MUX} in config.env)", out)
        self.assertIn(f"attach:  {self.MUX} attach prbot", out)
        self.assertEqual(self.names("prbot")[:2], ["home", "poller"])
        # Again: it's running, so it's used, not started twice.
        self.assertIn(f"poller is already running in {self.MUX} session 'prbot'", self.prbot("start"))
        self.assertIn(f"kill-session prbot", self.prbot("stop"))


def poller_pid(root):
    """The pid of the poller running from `root`, found through /proc."""
    want = os.path.join(root, "prbot.py")
    for pid in os.listdir("/proc"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if want.encode() in args and b"poll" in args:
            return int(pid)
    return None


def restore_env(key, old):
    if old is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = old


class Tos(MuxSession, unittest.TestCase):
    MUX = "tos"


class Zellij(MuxSession, unittest.TestCase):
    MUX = "zellij"


if __name__ == "__main__":
    unittest.main()
