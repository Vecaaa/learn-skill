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

    def test_export_checkpoints_the_wal(self):
        """export 之后通常紧跟着 git 提交 learn.db，所以 export 要把 WAL 合并回主文件。"""
        holder = sqlite3.connect(self.db)  # 保持连接，WAL 才不会在 run() 关闭连接时被自动合并
        holder.execute("SELECT COUNT(*) FROM atoms").fetchone()
        try:
            self.ldb.run(["add", "inwal", "只在 WAL 里"])
            wal = self.db + "-wal"
            self.assertGreater(os.path.getsize(wal), 0)  # 前提：数据确实还在 WAL 里
            self.ldb.run(["export", "--path", os.path.join(self._tmp.name, "k.md")])
            self.assertEqual(os.path.getsize(wal), 0)
        finally:
            holder.close()

    def test_failed_backup_leaves_no_partial_file(self):
        """备份中途失败留下的半成品会占 20 份保留额度，还会被误当成"最新备份"。"""
        real_connect = self.ldb.connect

        class FailingBackup:
            def __init__(self, con):
                self._con = con

            def __getattr__(self, name):
                return getattr(self._con, name)

            def backup(self, target):
                self._con.backup(target)
                raise sqlite3.OperationalError("disk full (simulated)")

        self.ldb.connect = lambda: FailingBackup(real_connect())
        with self.assertRaises(sqlite3.OperationalError):
            self.ldb.run(["backup"])
        self.assertEqual(os.listdir(os.path.join(self._tmp.name, "backups")), [])


if __name__ == "__main__":
    unittest.main()
