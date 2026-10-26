import importlib.util
import os
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

    def test_library_matches_golden(self):
        self.assertEqual(golden.run_lib_sequence(self.tmp), golden.load_golden())


if __name__ == "__main__":
    unittest.main()
