"""learndb 黄金基线：在库化改造前录制 CLI 的完整输出，改造后逐字比对。

覆盖 CLAUDE.md 里冒烟测试、大目标测试、回归测试 ①–⑦ 涉及的全部命令路径。
`session` 命令不在这里：M0 有意修改了它的行为，单独测试。

用法：
    python3 tests/golden.py --record     # 只在改代码之前跑一次
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
LEARNDB = os.path.join(SCRIPTS, "learndb.py")
GOLDEN = os.path.join(HERE, "golden", "cli.json")

# {TMP} 会被替换成本次运行的临时目录
CASES = [
    ["init"],
    ["add", "scope", "作用域", "--domain", "JS", "--alias", "scope"],
    ["add", "closure", "闭包", "--domain", "JS", "--prereq", "scope"],
    ["verify", "scope", "--result", "pass", "--method", "predict"],
    ["probe", "closure"],
    ["find", "作用域"],
    ["plan", "closure"],
    ["context", "JS"],
    ["verify", "closure", "--result", "fail", "--method", "predict"],
    ["misconception", "add", "--atom", "closure", "--wrong", "以为是全局变量",
     "--correction", "是词法环境的引用", "--error-type", "scope-confusion"],
    ["misconception", "list"],
    ["pref", "add", "--rule", "先代码后术语", "--scope", "domain:JS", "--evidence", "t1"],
    ["pref", "add", "--rule", "先代码后术语", "--scope", "domain:JS", "--evidence", "t2"],
    ["pref", "list"],
    ["get", "closure"],
    ["get", "nonexistent"],
    ["set", "closure", "notes=测试"],
    ["edge", "add", "scope", "closure", "--type", "related"],
    ["add", "typo", "拼错", "--prereq", "nonexistent"],
    ["probe", "typo"],
    ["doctor"],
    ["goal", "add", "g1", "测试目标", "--north-star", "能做出X", "--weekly-hours", "10"],
    ["phase", "add", "g1", "阶段1", "--seq", "1", "--objective", "能A",
     "--est-weeks", "4", "--detail", "detailed"],
    ["phase", "add", "g1", "阶段2", "--seq", "2", "--objective", "能B", "--est-weeks", "6"],
    ["phase", "list", "g1"],
    ["phase", "start", "1"],
    ["add", "a1", "atom1", "--phase", "1"],
    ["project", "add", "1", "p1", "项目1", "--scaffold", "训练循环已写好",
     "--user-scope", "你写 forward", "--atom", "a1"],
    ["project", "add", "999", "ghost", "幽灵", "--scaffold", "x", "--user-scope", "y"],
    ["phase", "get", "some-slug"],
    ["verify", "a1", "--result", "pass", "--method", "transfer"],
    ["project", "done", "p1"],
    ["phase", "close", "1", "--retro", "比预期快"],
    ["goal", "roadmap", "g1"],
    ["pref", "retire", "--id", "1", "--reason", "回归测试"],
    ["pref", "add", "--rule", "先代码后术语", "--scope", "domain:JS", "--evidence", "t3"],
    ["due"],
    ["distill"],
    ["review"],
    ["profile", "--set", "pace=扎实"],
    ["profile"],
    ["export", "--path", "{TMP}/k.md"],
    ["backup"],
    ["status"],
]

_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?")
_BACKUP = re.compile(r"learn-\d{8}-\d{6}\.db")


def normalize(obj, tmp):
    """去掉每次运行都不同的部分：临时路径、时间戳、备份文件名、浮点尾数。"""
    if isinstance(obj, dict):
        return {k: normalize(v, tmp) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize(v, tmp) for v in obj]
    if isinstance(obj, float):
        return round(obj, 3)
    if isinstance(obj, str):
        s = obj.replace(os.path.realpath(tmp), "<TMP>").replace(tmp, "<TMP>")
        s = _TS.sub("<TS>", s)
        return _BACKUP.sub("learn-<BK>.db", s)
    return obj


def expand(args, tmp):
    return [a.replace("{TMP}", tmp) for a in args]


def dump_db(path, tmp):
    """所有业务表的全量内容，用来发现"输出一样但库状态不一样"的回归。"""
    con = sqlite3.connect(path)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'atoms_fts%' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: normalize([list(r) for r in con.execute(f"SELECT * FROM {n} ORDER BY rowid")], tmp)
                for n in names}
    finally:
        con.close()


def run_cli_sequence(tmp):
    db = os.path.join(tmp, "learn.db")
    env = dict(os.environ, LEARN_DB=db)
    steps = []
    for args in CASES:
        p = subprocess.run([sys.executable, LEARNDB, *expand(args, tmp)],
                           capture_output=True, text=True, env=env)
        out = json.loads(p.stdout) if p.stdout.strip() else None
        steps.append({"args": args, "exit": p.returncode, "out": normalize(out, tmp)})
    return {"steps": steps, "db": dump_db(db, tmp)}


def run_lib_sequence(tmp):
    """与 run_cli_sequence 相同的命令序列，但走库调用入口 run()。"""
    import importlib.util
    db = os.path.join(tmp, "learn.db")
    spec = importlib.util.spec_from_file_location("learndb_golden", LEARNDB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.configure(db)
    steps = []
    for args in CASES:
        out = mod.run(expand(args, tmp))
        steps.append({"args": args, "exit": 0, "out": normalize(out, tmp)})
    return {"steps": steps, "db": dump_db(db, tmp)}


def load_golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    if "--record" not in sys.argv:
        sys.exit("只在改代码之前用 --record 录制一次")
    with tempfile.TemporaryDirectory() as tmp:
        result = run_cli_sequence(tmp)
    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"recorded {len(result['steps'])} steps -> {GOLDEN}")
