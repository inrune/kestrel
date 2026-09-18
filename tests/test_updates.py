"""The updater: what it refuses is more important than what it installs.

An update replaces the code that holds someone's money, so every one of
these is about the refusal path — a damaged download, an archive that is
not a Kestrel app, one that tries to write outside the folder, a version
that is not actually newer. The one thing an update must never do is leave
a working installation broken.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

from kestrel import updates


def make_app(root, *, version="1.4.8", marker="old", extra=None):
    """A believable installed app, wallet and settings included."""
    os.makedirs(os.path.join(root, "kestrel"), exist_ok=True)
    write(root, "app.py", f"MARKER = {marker!r}\n")
    write(root, os.path.join("kestrel", "__init__.py"),
          f'__version__ = "{version}"\n')
    for name, body in (extra or {}).items():
        write(root, name, body)
    return root


def write(root, rel, body):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)


def zip_of(src, arcroot="kestrel-wallet"):
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w") as z:
        for base, _dirs, files in os.walk(src):
            for f in files:
                p = os.path.join(base, f)
                z.write(p, os.path.join(arcroot, os.path.relpath(p, src)))
    return path


class TestVersions(unittest.TestCase):
    def test_ordering(self):
        self.assertTrue(updates.is_newer("v1.4.9", "1.4.8"))
        self.assertTrue(updates.is_newer("1.5.0", "1.4.99"))
        self.assertTrue(updates.is_newer("2.0", "1.9.9"))
        self.assertFalse(updates.is_newer("1.4.8", "1.4.8"))
        self.assertFalse(updates.is_newer("1.4.7", "1.4.8"))
        self.assertFalse(updates.is_newer("", "1.4.8"))
        self.assertFalse(updates.is_newer("garbage", "1.4.8"))

    def test_asset_for_this_app(self):
        assets = [{"name": "kestrel-core.zip", "url": "c", "size": 1},
                  {"name": "kestrel-miner.zip", "url": "m", "size": 1},
                  {"name": "kestrel-wallet.zip", "url": "w", "size": 1},
                  {"name": "SHA256SUMS.txt", "url": "s", "size": 1}]
        self.assertEqual(updates.pick_asset(assets, "kestrel-miner")["url"], "m")
        self.assertEqual(updates.pick_asset(assets, "kestrel-wallet")["url"], "w")
        self.assertIsNone(updates.pick_asset([], "kestrel-miner"))
        # never hand back a checksum file as if it were the app
        only_sums = [{"name": "SHA256SUMS.txt", "url": "s", "size": 1}]
        self.assertIsNone(updates.pick_asset(only_sums, "kestrel-miner"))

    def test_release_notes_become_plain_lines(self):
        out = updates.summarize(
            "## v1.4.9\n"
            "- **Fixed** the [pending bug](http://example.com)\n"
            "* Faster `refresh`\n"
            "\n"
            "| a | b |\n"
            "<!-- hidden -->\n")
        self.assertEqual(out, ["Fixed the pending bug", "Faster refresh"])
        self.assertEqual(updates.summarize(""), [])
        self.assertEqual(updates.summarize(None), [])

    def test_notes_never_carry_a_link_through(self):
        for line in updates.summarize("- click http://evil.example/x now"):
            self.assertNotIn("http", line)


class TestStaging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.staging = os.path.join(self.tmp, "staging")
        os.makedirs(self.staging)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _zip(self, **kw):
        src = make_app(os.path.join(self.tmp, "src"), **kw)
        return zip_of(src)

    def test_accepts_a_genuine_newer_build(self):
        z = self._zip(version="9.9.9", marker="new")
        root = updates.stage(z, self.staging, expect_version="v9.9.9")
        self.assertTrue(os.path.exists(os.path.join(root, "app.py")))

    def test_refuses_an_older_or_equal_build(self):
        with self.assertRaises(ValueError) as e:
            updates.stage(self._zip(version="0.0.1"), self.staging)
        self.assertIn("not\nnewer".replace("\n", " "), str(e.exception))

    def test_refuses_when_the_build_disagrees_with_the_release(self):
        z = self._zip(version="9.9.9")
        with self.assertRaises(ValueError):
            updates.stage(z, self.staging, expect_version="v9.9.8")

    def test_refuses_an_archive_that_is_not_a_kestrel_app(self):
        fd, z = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("something/readme.txt", "hello")
        with self.assertRaises(ValueError):
            updates.stage(z, self.staging)

    def test_refuses_a_path_escape(self):
        """A zip entry naming ../../ must never be written."""
        fd, z = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("kestrel-wallet/app.py", "x")
            f.writestr("kestrel-wallet/kestrel/__init__.py",
                       '__version__ = "9.9.9"')
            f.writestr("../../../../tmp/kestrel-pwned.txt", "owned")
        with self.assertRaises(ValueError) as e:
            updates.stage(z, self.staging)
        self.assertIn("unsafe path", str(e.exception))
        self.assertFalse(os.path.exists("/tmp/kestrel-pwned.txt"))

    def test_checksum_mismatch_is_fatal(self):
        src = os.path.join(self.tmp, "payload.bin")
        with open(src, "wb") as f:
            f.write(b"kestrel" * 100)
        dest = os.path.join(self.tmp, "out", "payload.bin")
        wrong = hashlib.sha256(b"something else").hexdigest()
        with self.assertRaises(ValueError):
            updates.download("file://" + src, dest, expect_sha256=wrong)
        self.assertFalse(os.path.exists(dest))          # nothing left behind
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_matching_checksum_is_accepted(self):
        src = os.path.join(self.tmp, "payload.bin")
        body = b"kestrel" * 100
        with open(src, "wb") as f:
            f.write(body)
        dest = os.path.join(self.tmp, "out", "payload.bin")
        got = updates.download("file://" + src, dest,
                               expect_sha256=hashlib.sha256(body).hexdigest())
        self.assertEqual(got, hashlib.sha256(body).hexdigest())
        self.assertTrue(os.path.exists(dest))


class TestApplying(unittest.TestCase):
    """The swap itself, run for real as its own process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = make_app(
            os.path.join(self.tmp, "app"), version="1.4.8", marker="old",
            extra={
                os.path.join("kestrel", "dropped.py"): "# gone next version\n",
                "kestrel-wallet.json": '{"private_key": "THE-MONEY"}',
                "wallet-settings.json": '{"geometry": "1000x680"}',
                os.path.join("kestrel-data", "chain.json"): '{"blocks": []}',
            })
        self.staging = os.path.join(self.app, ".kestrel-update")
        os.makedirs(self.staging, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, staged):
        """Apply, standing in a short-lived process for the running app."""
        script = os.path.join(self.staging, "apply_update.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(updates.APPLY_SCRIPT)
        stand_in = subprocess.Popen([sys.executable, "-c",
                                     "import time; time.sleep(1.5)"])
        cfg = os.path.join(self.staging, "apply.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"app_dir": self.app, "staged": staged,
                       "backup": os.path.join(self.staging, "previous"),
                       "keep": sorted(updates.KEEP),
                       "required": list(updates.REQUIRED),
                       "pid": stand_in.pid,
                       "relaunch": [sys.executable, "-c", "pass"]}, f)
        out = subprocess.run([sys.executable, script, cfg],
                             capture_output=True, text=True, cwd=self.staging,
                             timeout=180)
        stand_in.wait(timeout=30)
        return out.stdout

    def _read(self, rel):
        try:
            with open(os.path.join(self.app, rel), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None

    def test_a_good_update_swaps_code_and_leaves_the_money_alone(self):
        new = make_app(os.path.join(self.tmp, "new"), version="1.4.9",
                       marker="new",
                       extra={os.path.join("kestrel", "added.py"): "# new\n"})
        self._run(new)

        self.assertEqual(self._read("app.py"), "MARKER = 'new'")
        self.assertIn("1.4.9", self._read(os.path.join("kestrel",
                                                       "__init__.py")))
        self.assertIsNotNone(self._read(os.path.join("kestrel", "added.py")))
        self.assertIsNone(self._read(os.path.join("kestrel", "dropped.py")))

        # the three things an update must never touch
        self.assertEqual(self._read("kestrel-wallet.json"),
                         '{"private_key": "THE-MONEY"}')
        self.assertEqual(self._read("wallet-settings.json"),
                         '{"geometry": "1000x680"}')
        self.assertEqual(self._read(os.path.join("kestrel-data", "chain.json")),
                         '{"blocks": []}')

    def test_the_wallet_is_not_even_copied_into_the_backup(self):
        new = make_app(os.path.join(self.tmp, "new"), version="1.4.9")
        self._run(new)
        backup = os.path.join(self.staging, "previous")
        self.assertTrue(os.path.isdir(backup))
        self.assertFalse(os.path.exists(os.path.join(backup,
                                                     "kestrel-wallet.json")))

    def test_stale_bytecode_cannot_survive_an_update(self):
        """Python keeps a .pyc when the source's size and mtime match what
        it recorded. Two versions differing by one character, copied in the
        same second, match on both — so the old code would keep running and
        the update would appear to install while doing nothing."""
        import compileall
        compileall.compile_dir(os.path.join(self.app, "kestrel"), quiet=2)
        cached = os.path.join(self.app, "kestrel", "__pycache__")
        self.assertTrue(os.path.isdir(cached))

        new = make_app(os.path.join(self.tmp, "new"), version="1.4.9",
                       marker="new")
        self._run(new)
        self.assertFalse(os.path.isdir(cached))

        out = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {self.app!r}); "
             "import kestrel; print(kestrel.__version__)"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "1.4.9")

    def test_an_empty_staging_folder_cannot_wipe_the_app(self):
        """The dangerous case: delete the old files, install nothing."""
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        out = self._run(empty)
        self.assertIn("not a complete app", out)
        self.assertEqual(self._read("app.py"), "MARKER = 'old'")
        self.assertEqual(self._read("kestrel-wallet.json"),
                         '{"private_key": "THE-MONEY"}')

    def test_a_half_extracted_update_cannot_wipe_the_app(self):
        broken = os.path.join(self.tmp, "broken")
        os.makedirs(broken)
        write(broken, "app.py", "MARKER = 'half'\n")     # no kestrel/ package
        out = self._run(broken)
        self.assertIn("not a complete app", out)
        self.assertEqual(self._read("app.py"), "MARKER = 'old'")

    def test_it_waits_for_the_app_to_exit(self):
        """Replacing files under a running app is how you corrupt one."""
        new = make_app(os.path.join(self.tmp, "new"), version="1.4.9",
                       marker="new")
        script = os.path.join(self.staging, "apply_update.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(updates.APPLY_SCRIPT)
        cfg = os.path.join(self.staging, "apply.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"app_dir": self.app, "staged": new,
                       "backup": os.path.join(self.staging, "previous"),
                       "keep": sorted(updates.KEEP),
                       "required": list(updates.REQUIRED),
                       "pid": os.getpid(),          # this test, very much alive
                       "wait_seconds": 3,           # no need to sit out the full 60
                       "relaunch": [sys.executable, "-c", "pass"]}, f)
        out = subprocess.run([sys.executable, script, cfg],
                             capture_output=True, text=True,
                             cwd=self.staging, timeout=240)
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(self._read("app.py"), "MARKER = 'old'")


if __name__ == "__main__":
    unittest.main()
