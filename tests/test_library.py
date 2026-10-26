import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest

import golden


def load_learndb(db_path):
    """每个测试加载一份独立的模块对象，避免全局 DB_PATH 互相污染。"""
    spec = importlib.util.spec_from_file_location("learndb_under_test", golden.LEARNDB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.configure(db_path)
    return mod


class LibraryTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.db = os.path.join(self.tmp, "learn.db")
        self.ldb = load_learndb(self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_returns_object_instead_of_printing(self):
        self.assertTrue(self.ldb.run(["init"])["ok"])
        res = self.ldb.run(["add", "scope", "作用域", "--domain", "JS"])
        self.assertEqual(res["slug"], "scope")

    def test_business_error_is_returned_not_raised(self):
        self.ldb.run(["init"])
        self.assertEqual(self.ldb.run(["get", "nope"]), {"error": "not found", "slug": "nope"})

    def test_bad_arguments_raise_instead_of_exiting(self):
        self.ldb.run(["init"])
        with self.assertRaises(self.ldb.LearnDBError) as cm:
            self.ldb.run(["verify", "scope", "--result", "maybe", "--method", "predict"])
        self.assertIn("参数错误", cm.exception.payload["error"])

    def test_help_raises(self):
        with self.assertRaises(self.ldb.LearnDBError):
            self.ldb.run(["status", "--help"])

    def test_help_prints_nothing(self):
        """库模式下 --help 只抛 LearnDBError，不能往服务进程的 stdout/stderr 打印用法。"""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(self.ldb.LearnDBError):
                self.ldb.run(["status", "--help"])
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_connect_failure_does_not_leak_output_sink(self):
        """connect() 抛错时不能把收集器留在 ContextVar 里，否则之后的 out() 全被静默吞掉。"""
        blocker = os.path.join(self.tmp, "blocker")
        open(blocker, "w").close()
        self.ldb.configure(os.path.join(blocker, "sub", "x.db"))  # 父路径是文件，makedirs 必失败
        with self.assertRaises(OSError):
            self.ldb.run(["status"])
        self.assertIsNone(self.ldb._OUT_SINK.get())

    def test_relative_learn_db_env_works(self):
        """LEARN_DB 是相对路径（无目录部分）时，早期实现 makedirs('') 直接崩溃。"""
        env = dict(os.environ, LEARN_DB="rel.db")
        r = subprocess.run([sys.executable, golden.LEARNDB, "init"], cwd=self.tmp, env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "rel.db")))

    def test_uninitialized_db_raises(self):
        with self.assertRaises(self.ldb.LearnDBError) as cm:
            self.ldb.run(["status"])
        self.assertIn("未初始化", cm.exception.payload["error"])

    def test_non_string_argv_is_accepted(self):
        self.ldb.run(["init"])
        self.ldb.run(["add", "a", "A"])
        self.assertIn("hits", self.ldb.run(["find", "A", "--limit", 5]))

    def test_threads_do_not_mix_outputs(self):
        self.ldb.run(["init"])
        for i in range(8):
            self.ldb.run(["add", f"t{i}", f"T{i}"])
        results, errors = {}, []

        def worker(i):
            try:
                results[i] = self.ldb.run(["get", f"t{i}"])["slug"]
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, {i: f"t{i}" for i in range(8)})

    def test_threads_write_concurrently(self):
        self.ldb.run(["init"])
        errors = []

        def worker(i):
            for j in range(10):
                try:
                    self.ldb.run(["add", f"w{i}-{j}", "x"])
                except Exception as e:  # noqa: BLE001
                    errors.append(repr(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.ldb.run(["status"])["atoms_total"], 60)

    def test_library_matches_golden(self):
        self.assertEqual(golden.run_lib_sequence(self.tmp), golden.load_golden())


class SessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "learn.db")
        self.ldb = load_learndb(self.db)
        self.ldb.run(["init"])

    def tearDown(self):
        self._tmp.cleanup()

    def rows(self):
        import sqlite3
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT id, ended IS NOT NULL, summary FROM sessions ORDER BY id").fetchall()
        finally:
            con.close()

    def test_repeated_close_without_start_keeps_every_checkpoint(self):
        self.ldb.run(["session", "close", "--summary", "第一次"])
        self.ldb.run(["session", "close", "--summary", "第二次"])
        self.assertEqual(self.rows(), [(1, 1, "第一次"), (2, 1, "第二次")])

    def test_close_updates_the_single_open_session(self):
        sid = self.ldb.run(["session", "start", "--goal", "g"])["session_id"]
        res = self.ldb.run(["session", "close", "--summary", "s"])
        self.assertEqual(res["session_id"], sid)
        self.assertEqual(self.rows(), [(sid, 1, "s")])

    def test_close_with_several_open_sessions_requires_id(self):
        self.ldb.run(["session", "start"])
        self.ldb.run(["session", "start"])
        res = self.ldb.run(["session", "close", "--summary", "s"])
        self.assertIn("error", res)
        self.assertEqual([r[1] for r in self.rows()], [0, 0])

    def test_close_unknown_id_is_an_error(self):
        res = self.ldb.run(["session", "close", "--id", 99, "--summary", "s"])
        self.assertIn("error", res)
        self.assertEqual(self.rows(), [])

    def test_start_close_then_bare_close_starts_a_new_row(self):
        a = self.ldb.run(["session", "start"])["session_id"]
        self.ldb.run(["session", "close", "--summary", "一"])
        b = self.ldb.run(["session", "close", "--summary", "二"])["session_id"]
        self.assertNotEqual(a, b)
        self.assertEqual(self.rows(), [(a, 1, "一"), (b, 1, "二")])

    def test_close_with_explicit_id(self):
        a = self.ldb.run(["session", "start"])["session_id"]
        self.ldb.run(["session", "start"])
        self.ldb.run(["session", "close", "--id", a, "--summary", "s"])
        self.assertEqual(self.rows()[0], (a, 1, "s"))


if __name__ == "__main__":
    unittest.main()
