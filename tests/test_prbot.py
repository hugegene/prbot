"""Tests for prbot. Run them from the repo folder:

    python3 -m unittest discover -s tests -v

They work on a copy of prbot in a temporary folder, with its own config.env, so they never touch yours, your
state or your sessions. The multiplexer tests start a throwaway tos or zellij session, and are skipped when that
program isn't installed. Nothing reaches GitHub, and a stub stands in for Claude.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        self.assertEqual((m.MUX, m.SESSION, m.PANE_VAR), ("tos", "prbot", "TOS_PANE_ID"))

    def test_zellij_and_the_old_session_setting(self):
        m = self.module(MUX="zellij", ZELLIJ_SESSION="old")
        self.assertEqual((m.MUX, m.SESSION, m.SESSION_VAR), ("zellij", "old", "ZELLIJ_SESSION_NAME"))
        shutil.rmtree(os.path.join(self.tmp, "prbot"))
        self.assertEqual(self.module(SESSION="new", ZELLIJ_SESSION="old").SESSION, "new")

    def test_an_unknown_mux_stops_with_a_message(self):
        root = make_copy(self.tmp, {"MUX": "screen"})
        r = subprocess.run([sys.executable, os.path.join(root, "prbot.py"), "ls"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("MUX=screen in config.env; use tos or zellij", r.stderr)

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
        self.root = make_copy(self.tmp, {"GH_USER": "tester", "REPO_DIRS": f"acme/widgets={clone},acme/gone=/nowhere"})

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
    """The whole round on one multiplexer, as prbot uses it: start, a job's tab, its pane finding its own tab,
    "close the tab", and stop. A subclass sets MUX."""

    MUX = ""

    def setUp(self):
        if not shutil.which(self.MUX):
            self.skipTest(f"{self.MUX} isn't installed")
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.session = f"prbot-test-{os.getpid()}"
        env = {"HOME": os.path.join(self.tmp, "home")}
        os.makedirs(env["HOME"])
        # Sockets need short paths (about 100 bytes at most), so they go straight under /tmp.
        self.sockets = f"/tmp/prbt-{os.getpid()}-{self.MUX}"
        if self.MUX == "tos":
            env.update(TOS_SOCKET_DIR=self.sockets, TOS_STATE_DIR=os.path.join(self.tmp, "tos-state"))
        else:
            env.update(ZELLIJ_SOCKET_DIR=self.sockets)
        self.addCleanup(shutil.rmtree, self.sockets, True)
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
        self.root = make_copy(self.tmp, {"MUX": self.MUX, "SESSION": self.session, "GH_USER": "tester",
                                         "REPO_DIRS": f"acme/widgets={clone}", "CLOSE_GRACE_SECS": "1",
                                         "POLL_SECS": "3600"})
        with open(os.path.join(self.root, "prbot.py")) as f:
            src = f.read()
        with open(os.path.join(self.root, "prbot.py"), "w") as f:
            f.write(src.replace('API = "https://api.github.com"', 'API = "http://127.0.0.1:9"'))
        # Claude's stand-in writes down the variables the tab's pane has, then waits to be closed.
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.out)
        with open(os.path.join(self.root, "bin", "run-claude"), "w") as f:
            f.write(f'#!/bin/sh\nenv > "{self.out}/run-$3.env"\nexec sleep 300\n')
        self.addCleanup(self.end_session)
        self.m = load(self.root)

    def end_session(self):
        if self.MUX == "tos":
            self.m.mux("kill", self.session)
            self.m.mux("delete-session", "-f", self.session)
        else:
            self.m.mux("kill-session", self.session)
            self.m.mux("delete-session", "--force", self.session)

    def prbot(self, *args, env=None):
        r = subprocess.run([sys.executable, os.path.join(self.root, "prbot.py"), *args], capture_output=True,
                           text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def names(self):
        return [t["name"] for t in self.m.list_tabs()]

    def test_a_round(self):
        m = self.m
        # 1. start: a session whose first tab is "home", and the poller's tab.
        self.assertIn(f"poller started in {self.MUX} session '{self.session}'", self.prbot("start"))
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
        saved = {k: os.environ.get(k) for k in (m.PANE_VAR, m.SESSION_VAR)}
        try:
            os.environ.update({m.PANE_VAR: pane_env[m.PANE_VAR], m.SESSION_VAR: pane_env[m.SESSION_VAR]})
            self.assertEqual(m.own_tab_id(), tab)
        finally:
            for k, v in saved.items():
                restore_env(k, v)

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
