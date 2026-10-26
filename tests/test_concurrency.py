import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from test_library import load_learndb
import golden


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "learn.db")
        self.ldb = load_learndb(self.db)
        self.ldb.run(["init"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_database_is_in_wal_mode(self):
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            con.close()

    def test_two_processes_write_concurrently(self):
        code = (
            "import sys; sys.path.insert(0, %r); import learndb; learndb.configure(%r); "
            "[learndb.run(['add', f'{sys.argv[1]}-{n}', 'x']) for n in range(25)]"
        ) % (golden.SCRIPTS, self.db)
        procs = [subprocess.Popen([sys.executable, "-c", code, tag],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for tag in ("a", "b")]
        for p in procs:
            _, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err.decode())
        con = sqlite3.connect(self.db)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM atoms").fetchone()[0], 50)
        finally:
            con.close()

    def test_readonly_connection_sees_uncheckpointed_data(self):
        """resume skill 的 learn_bridge 用 mode=ro 打开 learn.db，WAL 下必须仍能读到最新数据。"""
        holder = sqlite3.connect(self.db)  # 保持一个连接打开，WAL 就不会在关闭时被合并删除
        holder.execute("SELECT COUNT(*) FROM atoms").fetchone()  # 必须真的用过连接：只 connect 不查询，SQLite 不认为它占用了库
        try:
            self.ldb.run(["add", "fresh", "刚写的"])
            ro = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            try:
                self.assertEqual(
                    ro.execute("SELECT name FROM atoms WHERE slug='fresh'").fetchone()[0], "刚写的")
            finally:
                ro.close()
        finally:
            holder.close()
        ro = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)  # 所有连接都关了以后
        try:
            self.assertEqual(ro.execute("SELECT COUNT(*) FROM atoms").fetchone()[0], 1)
        finally:
            ro.close()

    def test_backup_contains_uncheckpointed_data(self):
        holder = sqlite3.connect(self.db)
        holder.execute("SELECT COUNT(*) FROM atoms").fetchone()  # 同上：让 holder 真正占用库
        try:
            self.ldb.run(["add", "inwal", "只在 WAL 里"])
            path = self.ldb.run(["backup"])["backup"]
        finally:
            holder.close()
        bk = sqlite3.connect(path)
        try:
            self.assertEqual(bk.execute("SELECT COUNT(*) FROM atoms WHERE slug='inwal'").fetchone()[0], 1)
            self.assertEqual(bk.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        finally:
            bk.close()


if __name__ == "__main__":
    unittest.main()
