"""Tests for scripts/journal.py. Standard library only: python3 -m unittest discover -s tests"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "journal.py"
TODAY = "2026-10-03"


def tiny_png(path: Path) -> None:
    """Write a small valid PNG, so the inbox has a real image to work with."""
    w, h = 8, 8
    raw = b"".join(b"\x00" + b"\xc8\xbe\xaa" * w for _ in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class JournalTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "journal"
        self.env = dict(os.environ, MARGINALIA_HOME=str(self.home))

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, ok=True):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True)
        if ok and proc.returncode != 0:
            self.fail(f"journal.py {' '.join(args)} failed:\n{proc.stderr}")
        if not ok:
            self.assertNotEqual(proc.returncode, 0, f"expected journal.py {' '.join(args)} to fail")
        return proc

    def add(self, book="novel", result="got", today=TODAY, question="q"):
        return self.run_cli("add", "--book", book, "--question", question, "--key", "k",
                            "--source", "ch. 1", "--result", result, "--today", today)

    def items(self):
        return json.loads((self.home / "review-queue.json").read_text())["items"]

    def item(self, item_id):
        return next(it for it in self.items() if it["id"] == item_id)


class QueueTests(JournalTestCase):
    def test_first_review_depends_on_how_they_did(self):
        self.add(result="got")
        self.add(result="partial")
        self.add(result="missed")
        self.assertEqual([it["due"] for it in self.items()], ["2026-10-06", "2026-10-04", "2026-10-04"])

    def test_correct_answers_climb_the_boxes_then_retire(self):
        self.add(result="got")  # box 2, due in 3 days
        for day, expected_due in [("2026-10-06", "2026-10-13"), ("2026-10-13", "2026-11-03"),
                                  ("2026-11-03", "2027-01-02")]:
            self.run_cli("grade", "1", "got", "--today", day)
            self.assertEqual(self.item(1)["due"], expected_due)
        out = self.run_cli("grade", "#1", "got", "--today", "2027-01-02").stdout
        self.assertIn("retired", out)
        self.assertEqual(self.item(1)["retired"], "2027-01-02")
        self.run_cli("grade", "1", "got", ok=False)

    def test_partial_repeats_the_box_and_missed_starts_over(self):
        self.add(result="got")
        self.run_cli("grade", "1", "got", "--today", "2026-10-06")  # box 3
        self.run_cli("grade", "1", "partial", "--today", "2026-10-13")
        self.assertEqual((self.item(1)["box"], self.item(1)["due"]), (3, "2026-10-20"))
        self.run_cli("grade", "1", "missed", "--today", "2026-10-20")
        self.assertEqual((self.item(1)["box"], self.item(1)["due"]), (1, "2026-10-21"))

    def test_due_mixes_books_and_respects_the_limit(self):
        for i in range(3):
            self.add(book="novel", result="missed", question=f"n{i}")
        self.add(book="essays", result="missed", question="e0")
        out = self.run_cli("due", "--limit", "2", "--today", "2026-10-04").stdout
        self.assertIn("4 due (showing 2)", out)
        self.assertIn("novel", out)
        self.assertIn("essays", out)
        self.assertIn("Nothing due.", self.run_cli("due", "--today", TODAY).stdout)

    def test_dropped_questions_leave_due_and_retired_counts(self):
        self.add(result="missed")
        self.run_cli("drop", "1", "--today", TODAY)
        self.assertIn("Nothing due.", self.run_cli("due", "--today", "2026-10-04").stdout)
        stats = self.run_cli("stats", "--today", "2026-10-04").stdout
        self.assertIn("Active: 0 · due now: 0 · retired (yours now): 0", stats)
        self.assertIn("dropped", self.run_cli("list", "--all").stdout)

    def test_stats_count_only_the_window_up_to_today(self):
        self.add(result="missed")
        self.run_cli("grade", "1", "partial", "--today", "2026-10-04")
        self.run_cli("grade", "1", "got", "--today", "2026-10-30")  # future relative to the check below
        stats = self.run_cli("stats", "--today", "2026-10-05").stdout
        self.assertIn("Reruns answered: last 7 days 1", stats)

    def test_bad_input_fails_cleanly(self):
        self.run_cli("grade", "99", "got", ok=False)
        self.run_cli("grade", "abc", "got", ok=False)
        self.run_cli("add", "--book", "b", "--question", "q", "--key", "k", "--source", "s",
                     "--result", "maybe", ok=False)
        self.run_cli("due", "--today", "10/3/2026", ok=False)

    def test_corrupt_queue_is_reported_not_overwritten(self):
        self.home.mkdir(parents=True)
        (self.home / "review-queue.json").write_text("{oops")
        self.run_cli("due", ok=False)
        self.assertEqual((self.home / "review-queue.json").read_text(), "{oops")

    @unittest.skipIf(os.name == "nt", "queue writes are only locked on POSIX")
    def test_simultaneous_writes_keep_every_question(self):
        procs = [subprocess.Popen([sys.executable, str(SCRIPT), "add", "--book", f"b{i % 3}",
                                   "--question", f"q{i}", "--key", "k", "--source", "s",
                                   "--result", "partial", "--today", TODAY],
                                  env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                 for i in range(15)]
        for p in procs:
            self.assertEqual(p.wait(), 0)
        self.assertEqual(sorted(it["id"] for it in self.items()), list(range(1, 16)))


class JournalSetupTests(JournalTestCase):
    def test_where_reports_a_missing_journal(self):
        out = self.run_cli("where").stdout
        self.assertIn(str(self.home), out)
        self.assertIn("run init", out)

    def test_init_creates_the_journal_and_never_overwrites(self):
        out = self.run_cli("init").stdout
        self.assertIn("Journal ready", out)
        for name in ("shelf.md", "profile.md", "README.md", "commonplace.md", "writing.md", "reflections.md"):
            self.assertTrue((self.home / name).is_file(), name)
        for folder in ("books", "plans", "inbox/done"):
            self.assertTrue((self.home / folder).is_dir(), folder)
        (self.home / "shelf.md").write_text("my shelf")
        self.assertIn("nothing changed", self.run_cli("init").stdout)
        self.assertEqual((self.home / "shelf.md").read_text(), "my shelf")
        self.assertNotIn("run init", self.run_cli("where").stdout)

    def test_bundled_plans_can_be_listed_and_copied_once(self):
        self.assertIn("harvard-classics", self.run_cli("plans").stdout)
        self.run_cli("add-plan", "harvard-classics")
        copied = self.home / "plans" / "harvard-classics.md"
        self.assertIn("### Jan 1 ·", copied.read_text())
        copied.write_text("my progress notes")
        self.assertIn("left it alone", self.run_cli("add-plan", "harvard-classics").stdout)
        self.assertEqual(copied.read_text(), "my progress notes")
        self.run_cli("add-plan", "no-such-plan", ok=False)


class InboxTests(JournalTestCase):
    def test_inbox_lists_readable_files_and_skips_the_rest(self):
        inbox = self.home / "inbox"
        inbox.mkdir(parents=True)
        tiny_png(inbox / "page-12.png")
        (inbox / "notes.docx").write_text("not a page")
        (inbox / ".DS_Store").write_text("")
        out = self.run_cli("inbox").stdout
        self.assertIn("1 file(s) ready", out)
        self.assertIn("page-12.png", out)
        self.assertIn("skipped notes.docx", out)
        self.assertNotIn(".DS_Store", out)

    def test_archive_moves_files_and_avoids_name_clashes(self):
        inbox = self.home / "inbox"
        inbox.mkdir(parents=True)
        tiny_png(inbox / "p.png")
        self.run_cli("archive", "p.png", "--today", TODAY)
        tiny_png(inbox / "p.png")
        self.run_cli("archive", "--all", "--today", TODAY)
        done = sorted(f.name for f in (inbox / "done" / TODAY).iterdir())
        self.assertEqual(done, ["p-1.png", "p.png"])
        self.assertIn("Inbox is empty.", self.run_cli("inbox").stdout)

    def test_archive_refuses_files_outside_the_inbox(self):
        outside = self.tmp / "elsewhere.png"
        tiny_png(outside)
        self.run_cli("archive", str(outside), ok=False)
        self.assertTrue(outside.exists())
        self.run_cli("archive", ok=False)

    @unittest.skipUnless(shutil.which("sips"), "HEIC conversion uses macOS sips")
    def test_iphone_heic_photos_become_jpegs(self):
        inbox = self.home / "inbox"
        inbox.mkdir(parents=True)
        tiny_png(self.tmp / "src.png")
        made = subprocess.run(["sips", "-s", "format", "heic", str(self.tmp / "src.png"),
                               "--out", str(inbox / "IMG_0001.HEIC")], capture_output=True)
        if made.returncode != 0:
            self.skipTest("this Mac's sips can't write HEIC")
        out = self.run_cli("inbox", "--today", TODAY).stdout
        self.assertIn("IMG_0001.jpg", out)
        self.assertTrue((inbox / "IMG_0001.jpg").is_file())
        self.assertTrue((inbox / "done" / TODAY / "IMG_0001.HEIC").is_file())


if __name__ == "__main__":
    unittest.main()
