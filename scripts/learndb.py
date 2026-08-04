#!/usr/bin/env python3
"""learndb — learn skill 的记忆层。

唯一真源是 SQLite（默认 ~/.claude/learning/learn.db，可用 LEARN_DB 覆盖）。
模型不直接写 SQL，所有读写走这里的子命令，schema 因此不会漂移。

设计要点见 references/memory-schema.md。快速上手：
    python3 learndb.py init
    python3 learndb.py status
    python3 learndb.py <cmd> --help
"""

import argparse
import json
import math
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------- 基础设施

DEFAULT_DIR = os.path.expanduser("~/.claude/learning")
DB_PATH = os.environ.get("LEARN_DB", os.path.join(DEFAULT_DIR, "learn.db"))

# 检验强度分级：只有 transfer（迁移到新情境）才配得上 level 4。
# 复述能过不代表会用，这条映射是掌握度不虚高的关键。
METHOD_LEVEL = {
    "recall": 1,     # 复述定义
    "judge": 2,      # 判断/选择
    "predict": 3,    # 预测输出
    "debug": 3,      # 找错
    "transfer": 4,   # 迁移到新情境
}
LEVEL_NAME = {
    0: "未学", 1: "见过", 2: "能复述", 3: "能应用", 4: "能迁移",
}
PROBE_THRESHOLD = 0.7   # 估算回忆概率低于此值 → 教学前值得探针
DUE_THRESHOLD = 0.6     # 按需复习模式里认定"已经开始忘"的线


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def days_since(ts):
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then.astimezone(timezone.utc)).total_seconds() / 86400)


def recall_prob(last_verified, stability):
    """指数遗忘曲线 R = exp(-t/S)。S 是稳定度（天），每次验证通过会变大。

    这不是精确的记忆科学，只是一个够用的排序信号：它的作用是决定
    "要不要在开讲前探一下"，而不是替代真实的检验。
    """
    t = days_since(last_verified)
    if t is None:
        return 0.0
    s = max(0.5, float(stability or 1.0))
    return round(math.exp(-t / s), 3)


# schema 版本。加表/加列时 +1，并在 migrate() 里补上对应的升级动作。
# migrate() 靠它走快速路径，避免每条命令都重跑一遍全量 DDL。
SCHEMA_VERSION = 2


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    # 注意：这里刻意不开 PRAGMA foreign_keys。表结构里没有 REFERENCES 声明
    # （加 FK 需要重建已有表，代价大于收益），引用完整性靠两层保证：
    # 写入时校验（project add 会拒绝不存在的 phase）+ doctor 命令定期体检。
    # 早期版本这里开了 PRAGMA 但一个 FK 都没声明，是纯误导，已移除。
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY,        -- background / goals / time_budget / pace /
    value TEXT,                  -- math_tolerance / language / 任意自定义键
    updated TEXT
);

CREATE TABLE IF NOT EXISTS atoms (
    slug TEXT PRIMARY KEY,       -- 稳定标识，kebab-case
    name TEXT NOT NULL,
    domain TEXT,
    aliases TEXT,                -- 换行分隔；中英文别名、俗称、错叫法都往里塞
    user_phrasing TEXT,          -- 用户自己复述时的原话，后续教学复用它
    level INTEGER DEFAULT 0,     -- 0..4，见 LEVEL_NAME
    stability REAL DEFAULT 1.0,  -- 遗忘曲线的 S（天）
    last_verified TEXT,
    verify_method TEXT,
    source TEXT,
    status TEXT DEFAULT 'active',-- active / blocked / retired
    fail_streak INTEGER DEFAULT 0,
    notes TEXT,
    phase_id INTEGER,             -- 属于哪个大目标的哪个阶段，可为空（非 goal 模式下用不到）
    created TEXT,
    updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_atoms_phase ON atoms(phase_id);

-- 大目标模式：goal（跨月的大目标）→ phase（里程碑）→ project（巩固项目）
-- 只有当用户提出"学一年做出 X"这类大目标时才用得到，日常单点学习不涉及这三张表。
CREATE TABLE IF NOT EXISTS goals (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    north_star TEXT,             -- 目标澄清后"成功具体长什么样"，防止"字面意义"的误解
    weekly_hours REAL,           -- 用户每周可投入时间，仅用于估算，不是承诺
    status TEXT DEFAULT 'active',-- active / paused / done
    notes TEXT,
    created TEXT, updated TEXT
);

CREATE TABLE IF NOT EXISTS phases (
    id INTEGER PRIMARY KEY,
    goal_slug TEXT NOT NULL,
    seq INTEGER NOT NULL,         -- 顺序，从 1 开始
    name TEXT NOT NULL,
    objective TEXT,               -- 验收标准："到这一步你应该能..."，不是日期
    detail_level TEXT DEFAULT 'coarse', -- coarse=只有里程碑名 / detailed=已展开到 atom+project
    est_weeks REAL,               -- 软性估计，仅供参考，会在 phase close 时校准
    actual_weeks REAL,
    status TEXT DEFAULT 'upcoming', -- upcoming / current / done / skipped
    started TEXT, ended TEXT,
    retrospective TEXT,
    created TEXT
);
CREATE INDEX IF NOT EXISTS idx_phases_goal ON phases(goal_slug, seq);

CREATE TABLE IF NOT EXISTS projects (
    slug TEXT PRIMARY KEY,
    phase_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    purpose TEXT,                 -- 这个项目巩固/应用了什么
    scaffold_provided TEXT,       -- 已经帮用户写好的部分，及为什么（工程量大但不是本阶段要学的东西）
    user_scope TEXT,              -- 用户自己要设计/写的那一小块，对应哪些知识点
    est_effort TEXT,              -- 粗略工作量描述，如"1-2 次学习会话"；体量必须小
    status TEXT DEFAULT 'planned',-- planned / in_progress / done / skipped
    artifact_path TEXT,
    started TEXT, completed TEXT,
    created TEXT, updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_projects_phase ON projects(phase_id);

CREATE TABLE IF NOT EXISTS project_atoms (
    project_slug TEXT NOT NULL,
    atom_slug TEXT NOT NULL,
    PRIMARY KEY (project_slug, atom_slug)
);

CREATE TABLE IF NOT EXISTS edges (
    from_slug TEXT NOT NULL,     -- from 是 to 的前置
    to_slug   TEXT NOT NULL,
    type TEXT DEFAULT 'prereq',  -- prereq / related
    PRIMARY KEY (from_slug, to_slug, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_slug);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_slug);

CREATE TABLE IF NOT EXISTS misconceptions (
    id INTEGER PRIMARY KEY,
    atom_slug TEXT,
    wrong_belief TEXT NOT NULL,  -- 用户当时的错误说法，尽量保留原话
    correction TEXT NOT NULL,
    first_seen TEXT, last_seen TEXT,
    recurrence INTEGER DEFAULT 1,
    resolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_misc_atom ON misconceptions(atom_slug);

CREATE TABLE IF NOT EXISTS teaching_prefs (
    id INTEGER PRIMARY KEY,
    layer TEXT DEFAULT 'L2',     -- L1=用户模型(自动生效) L2=教学策略(需证据)
    scope TEXT DEFAULT 'global', -- global / domain:math / atom:closure
    rule TEXT NOT NULL,
    polarity TEXT DEFAULT 'works',-- works / fails，失败教法同样要记
    evidence TEXT,               -- JSON 数组
    evidence_count INTEGER DEFAULT 1,
    active INTEGER DEFAULT 0,
    hit_count INTEGER DEFAULT 0,
    last_hit TEXT,
    created TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    started TEXT, ended TEXT,
    goal TEXT,
    atoms_touched TEXT,
    summary TEXT,
    signals TEXT
);

-- trigram 分词器对中文有效（unicode61 不切中文词），保证"我忘了那个栈的东西"
-- 这类模糊说法也能定位到 atom。
CREATE VIRTUAL TABLE IF NOT EXISTS atoms_fts USING fts5(
    slug, name, aliases, user_phrasing, notes, tokenize='trigram'
);
"""


def reindex(con, slug=None):
    if slug:
        con.execute("DELETE FROM atoms_fts WHERE slug=?", (slug,))
        rows = con.execute("SELECT * FROM atoms WHERE slug=?", (slug,)).fetchall()
    else:
        con.execute("DELETE FROM atoms_fts")
        rows = con.execute("SELECT * FROM atoms").fetchall()
    for r in rows:
        con.execute(
            "INSERT INTO atoms_fts(slug,name,aliases,user_phrasing,notes) VALUES(?,?,?,?,?)",
            (r["slug"], r["name"] or "", r["aliases"] or "", r["user_phrasing"] or "", r["notes"] or ""),
        )


def migrate(con):
    """给已存在的库补上后来加的列/表，保证老库升级到新 schema 是无损的。

    靠 meta.schema_version 走快速路径：版本已是最新就直接返回，
    不重跑 DDL。早期版本每条命令都 executescript 一遍全量 SCHEMA，
    纯属浪费——这个函数在每条非 init 命令前都会被调用。
    """
    try:
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row["value"]) if row else 0
    except (sqlite3.OperationalError, ValueError, TypeError):
        current = 0
    if current >= SCHEMA_VERSION:
        return
    cols = {r[1] for r in con.execute("PRAGMA table_info(atoms)")}
    if "phase_id" not in cols:
        con.execute("ALTER TABLE atoms ADD COLUMN phase_id INTEGER")
    con.executescript(SCHEMA)  # 全部 IF NOT EXISTS，重复执行安全
    con.execute("INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
    con.commit()


def phase_progress(con, phase_id):
    """阶段进度。刻意区分两个口径：level>=3 是"能应用"，level 4 才是"能迁移"。

    早期版本把 level>=3 直接叫 mastered，和"只有 transfer 通过才算真掌握"
    这条核心原则打架，看进度的人会高估自己。
    """
    atoms = con.execute("SELECT level FROM atoms WHERE phase_id=?", (phase_id,)).fetchall()
    projs = con.execute("SELECT status FROM projects WHERE phase_id=?", (phase_id,)).fetchall()
    return {
        "atoms_total": len(atoms),
        "atoms_applicable": sum(1 for a in atoms if a["level"] >= 3),   # 能应用
        "atoms_transfer_proven": sum(1 for a in atoms if a["level"] >= 4),  # 真掌握
        "projects_total": len(projs),
        "projects_done": sum(1 for p in projs if p["status"] == "done"),
    }


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def effective_recall(level, last_verified, stability, fail_streak=0):
    """当前回忆概率的统一算法。任何地方要算 recall 都走这里，不要各写各的。

    两个修正，都是为了不让"看起来很牢"掩盖真实状态：
    - level<2：回忆概率没有意义（刚答错的 atom 也会显示"刚验证过"），置 0
    - 最近失败过（fail_streak>0）：置 0。否则 verify --result fail 会把
      last_verified 刷成当下，recall 反而变成 1.0，导致 probe 跳过一个
      用户刚刚答错的前置——这是最该探的情况，不能漏。
    """
    if (level or 0) < 2 or (fail_streak or 0) > 0:
        return 0.0
    return recall_prob(last_verified, stability)


def _col(r, name, default=None):
    """安全取列：老库/局部 SELECT 可能没有这一列。"""
    try:
        return r[name]
    except (IndexError, KeyError):
        return default


def atom_view(r):
    fs = _col(r, "fail_streak", 0)
    p = effective_recall(r["level"], r["last_verified"], _col(r, "stability", 1.0), fs)
    return {
        "slug": r["slug"], "name": r["name"], "domain": _col(r, "domain"),
        "level": r["level"], "level_name": LEVEL_NAME.get(r["level"], "?"),
        "recall": p, "last_verified": r["last_verified"],
        "verify_method": _col(r, "verify_method"), "status": _col(r, "status"),
        "fail_streak": fs,
        "stale": p < PROBE_THRESHOLD,
    }


# ---------------------------------------------------------------- 命令

def cmd_init(a, con):
    con.executescript(SCHEMA)
    con.execute("INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))
    con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('created',?)", (now(),))
    con.commit()
    out({"ok": True, "db": DB_PATH, "schema_version": SCHEMA_VERSION})


def cmd_status(a, con):
    prof = {r["key"]: r["value"] for r in con.execute("SELECT * FROM profile")}
    counts = dict(con.execute(
        "SELECT status, COUNT(*) FROM atoms GROUP BY status").fetchall())
    by_level = dict(con.execute(
        "SELECT level, COUNT(*) FROM atoms GROUP BY level").fetchall())
    domains = [r[0] for r in con.execute(
        "SELECT domain, COUNT(*) c FROM atoms WHERE domain IS NOT NULL "
        "GROUP BY domain ORDER BY c DESC").fetchall()]
    last = con.execute(
        "SELECT started, goal FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
    out({
        "db": DB_PATH,
        "profile": prof,
        "atoms_total": sum(counts.values()),
        "atoms_by_status": counts,
        "atoms_by_level": {LEVEL_NAME.get(k, k): v for k, v in by_level.items()},
        "domains": domains,
        "misconceptions_open": con.execute(
            "SELECT COUNT(*) FROM misconceptions WHERE resolved=0").fetchone()[0],
        "active_prefs": con.execute(
            "SELECT COUNT(*) FROM teaching_prefs WHERE active=1").fetchone()[0],
        "last_session": dict(last) if last else None,
        "active_goals": _goal_summaries(con),
    })


def _goal_summaries(con):
    out_list = []
    for g in con.execute("SELECT * FROM goals WHERE status='active'").fetchall():
        cur = con.execute("SELECT * FROM phases WHERE goal_slug=? AND status='current'",
                          (g["slug"],)).fetchone()
        out_list.append({
            "slug": g["slug"], "name": g["name"],
            "current_phase": cur["name"] if cur else None,
            "phase_objective": cur["objective"] if cur else None,
            "progress": phase_progress(con, cur["id"]) if cur else None,
        })
    return out_list


def cmd_profile(a, con):
    if a.set:
        for kv in a.set:
            k, _, v = kv.partition("=")
            con.execute(
                "INSERT INTO profile(key,value,updated) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
                (k.strip(), v.strip(), now()))
        con.commit()
    out({r["key"]: r["value"] for r in con.execute("SELECT * FROM profile")})


def cmd_find(a, con):
    """模糊定位 atom —— 按需复习入口（"我忘了那个 XX"）的核心。"""
    q = a.query.strip()
    hits, seen = [], set()
    if len(q) >= 3:
        try:
            for r in con.execute(
                "SELECT slug FROM atoms_fts WHERE atoms_fts MATCH ? LIMIT 40",
                    ('"' + q.replace('"', '') + '"',)):
                seen.add(r["slug"])
        except sqlite3.OperationalError:
            pass
    like = f"%{q}%"
    for r in con.execute(
        "SELECT slug FROM atoms WHERE name LIKE ? OR slug LIKE ? OR aliases LIKE ? "
            "OR user_phrasing LIKE ? LIMIT 40", (like, like, like, like)):
        seen.add(r["slug"])
    for slug in seen:
        r = con.execute("SELECT * FROM atoms WHERE slug=?", (slug,)).fetchone()
        if r:
            hits.append(atom_view(r))
    hits.sort(key=lambda x: (-x["level"], x["recall"]))
    out({"query": q, "count": len(hits), "hits": hits[:a.limit]})


def cmd_get(a, con):
    r = con.execute("SELECT * FROM atoms WHERE slug=?", (a.slug,)).fetchone()
    if not r:
        out({"error": "not found", "slug": a.slug}); return
    d = dict(r); d.update(atom_view(r))
    d["prereqs"] = [dict(x) for x in con.execute(
        "SELECT a.slug,a.name,a.level,a.last_verified,a.stability,a.fail_streak FROM edges e "
        "JOIN atoms a ON a.slug=e.from_slug WHERE e.to_slug=? AND e.type='prereq'",
        (a.slug,))]
    for p in d["prereqs"]:
        # 走统一算法，别在这里另算一遍——早期版本这里漏了 level<2 / 失败过
        # 的修正，同一个 atom 在 get 和 probe 里会显示两个不同的 recall。
        p["recall"] = effective_recall(p["level"], p["last_verified"],
                                       p["stability"], p["fail_streak"])
    d["unlocks"] = [x["to_slug"] for x in con.execute(
        "SELECT to_slug FROM edges WHERE from_slug=? AND type='prereq'", (a.slug,))]
    d["misconceptions"] = [dict(x) for x in con.execute(
        "SELECT * FROM misconceptions WHERE atom_slug=? ORDER BY resolved, last_seen DESC",
        (a.slug,))]
    out(d)


def cmd_probe(a, con):
    """列出教 <slug> 之前值得探针的直接前置。

    只看直接前置、不向上追溯，是刻意的：探针是教学决策，不是复习课。
    往上追一层就变成"我是来学新东西的你怎么让我做旧题"。
    """
    edge_rows = con.execute(
        "SELECT from_slug FROM edges WHERE to_slug=? AND type='prereq'", (a.slug,)).fetchall()
    need, ok, dangling = [], [], []
    for e in edge_rows:
        r = con.execute("SELECT * FROM atoms WHERE slug=?", (e["from_slug"],)).fetchone()
        if r is None:
            # 依赖边指向一个不存在的 atom（多半是 --prereq 拼错了）。
            # 早期版本这里用 JOIN，悬空边会被静默丢掉，探针永远空转且无人察觉。
            dangling.append(e["from_slug"])
            continue
        v = atom_view(r)
        # 没学扎实（level<=2）→ 一定探；学扎实了但估计开始忘 → 也探；
        # 最近答错过 → 更要探。刚验证通过的高掌握度前置才跳过，
        # 否则探针会退化成每节课前的例行考试。
        if r["level"] <= 2 or v["recall"] < PROBE_THRESHOLD or r["fail_streak"] > 0:
            v["why"] = ("从未学过" if r["level"] == 0 else
                        f"上次检验失败（连续 {r['fail_streak']} 次）" if r["fail_streak"] > 0 else
                        f"只到「{v['level_name']}」，未必撑得住新内容" if r["level"] <= 2 else
                        f"估算回忆概率 {v['recall']}，可能已开始遗忘")
            need.append(v)
        else:
            ok.append(v)
    res = {"target": a.slug, "probe": need, "skip": ok,
           "hint": "对 probe 列表每项出 1 道题；过了立刻继续，不过就地补 2-3 分钟"}
    if dangling:
        res["dangling_prereqs"] = dangling
        res["warning"] = ("这些前置在库里不存在（多半是 slug 拼错）：" + ", ".join(dangling) +
                          "。请用 add 补建，或用 edge rm 删掉这条边——否则这些前置永远不会被探到")
    out(res)


def cmd_plan(a, con):
    """递归取出目标 atom 的全部前置闭包，按拓扑序（最深的祖先在前）返回。"""
    rows = con.execute("""
        WITH RECURSIVE anc(slug, depth) AS (
            SELECT from_slug, 1 FROM edges WHERE to_slug=? AND type='prereq'
            UNION
            SELECT e.from_slug, anc.depth+1 FROM edges e
            JOIN anc ON e.to_slug = anc.slug
            WHERE e.type='prereq' AND anc.depth < 12
        )
        SELECT a.*, MAX(anc.depth) d FROM anc JOIN atoms a ON a.slug=anc.slug
        GROUP BY a.slug ORDER BY d DESC
    """, (a.slug,)).fetchall()
    items = []
    for r in rows:
        v = atom_view(r)
        v["depth"] = r["d"]
        if a.gaps_only and r["level"] >= 3 and v["recall"] >= PROBE_THRESHOLD:
            continue
        items.append(v)
    target = con.execute("SELECT * FROM atoms WHERE slug=?", (a.slug,)).fetchone()
    out({"target": atom_view(target) if target else a.slug,
         "prereq_chain": items, "count": len(items)})


def cmd_context(a, con):
    """按主题切一个小上下文包出来。

    库大了不能整个塞进上下文——注意力被稀释，教学判断质量会明显下降。
    这个命令就是"只取相关子图"的那把刀。
    """
    like = f"%{a.topic}%"
    atoms = con.execute(
        "SELECT * FROM atoms WHERE domain LIKE ? OR name LIKE ? OR aliases LIKE ? "
        "OR slug LIKE ? ORDER BY level DESC LIMIT ?",
        (like, like, like, like, a.limit)).fetchall()
    slugs = [r["slug"] for r in atoms]
    qmarks = ",".join("?" * len(slugs)) or "''"
    misc = con.execute(
        f"SELECT atom_slug, wrong_belief, correction, recurrence FROM misconceptions "
        f"WHERE resolved=0 AND (atom_slug IN ({qmarks}) OR atom_slug IS NULL) "
        f"ORDER BY recurrence DESC LIMIT 15", slugs).fetchall()
    prefs = con.execute(
        "SELECT layer, scope, rule, polarity, evidence_count FROM teaching_prefs "
        "WHERE active=1 AND (scope='global' OR scope LIKE ?) "
        "ORDER BY evidence_count DESC LIMIT 20", (f"%{a.topic}%",)).fetchall()
    out({
        "topic": a.topic,
        "profile": {r["key"]: r["value"] for r in con.execute("SELECT * FROM profile")},
        "known_atoms": [atom_view(r) for r in atoms],
        "open_misconceptions": [dict(r) for r in misc],
        "teaching_rules": [dict(r) for r in prefs],
    })


def cmd_add(a, con):
    ts = now()
    con.execute(
        "INSERT INTO atoms(slug,name,domain,aliases,notes,source,phase_id,created,updated) "
        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
        "name=excluded.name, domain=COALESCE(excluded.domain,atoms.domain), "
        "phase_id=COALESCE(excluded.phase_id,atoms.phase_id), updated=excluded.updated",
        (a.slug, a.name, a.domain, "\n".join(a.alias or []), a.notes, a.source, a.phase, ts, ts))
    for p in (a.prereq or []):
        con.execute("INSERT OR IGNORE INTO edges(from_slug,to_slug,type) VALUES(?,?,'prereq')",
                    (p, a.slug))
    for p in (a.related or []):
        con.execute("INSERT OR IGNORE INTO edges(from_slug,to_slug,type) VALUES(?,?,'related')",
                    (a.slug, p))
    reindex(con, a.slug)
    con.commit()
    res = {"ok": True, "slug": a.slug, "prereqs": a.prereq or []}
    # 自顶向下建依赖图时，先连边后建 atom 是合法用法，所以不硬性拒绝；
    # 但必须报出来——静默接受拼错的 slug 会让探针永久空转。
    unknown = [p for p in (a.prereq or []) + (a.related or [])
               if not con.execute("SELECT 1 FROM atoms WHERE slug=?", (p,)).fetchone()]
    if unknown:
        res["unknown_refs"] = unknown
        res["warning"] = ("这些关联 atom 还不存在：" + ", ".join(unknown) +
                          "。如果是打算稍后补建就没问题；如果是 slug 拼错了，"
                          "现在就用 edge rm 删掉，否则探针永远探不到它")
    out(res)


def cmd_verify(a, con):
    """记一次检验结果，并按结果调整掌握度与稳定度。

    通过：level 提到该检验方式对应的上限，stability 放大（越熟放大越多）。
    失败：level 压到该方式之下，stability 大幅回落，连续 3 次失败标 blocked
          —— 硬刚同一个点是最伤学习动机的事，该止损就止损。
    """
    r = con.execute("SELECT * FROM atoms WHERE slug=?", (a.slug,)).fetchone()
    if not r:
        out({"error": "not found", "slug": a.slug}); return
    ml = METHOD_LEVEL[a.method]
    lvl, stab, streak, status = r["level"], float(r["stability"] or 1.0), r["fail_streak"], r["status"]
    if a.result == "pass":
        lvl = max(lvl, ml)
        stab = round(min(365.0, max(1.0, stab) * (1.4 + 0.25 * lvl)), 2)
        streak = 0
        status = "active" if status == "blocked" else status
    else:
        lvl = max(0, min(lvl, ml - 1))
        stab = round(max(0.5, stab * 0.4), 2)
        streak += 1
        if streak >= 3:
            status = "blocked"
    con.execute(
        "UPDATE atoms SET level=?, stability=?, last_verified=?, verify_method=?, "
        "fail_streak=?, status=?, user_phrasing=COALESCE(?,user_phrasing), updated=? WHERE slug=?",
        (lvl, stab, now(), a.method, streak, status, a.phrasing, now(), a.slug))
    con.commit()
    reindex(con, a.slug); con.commit()
    res = {"slug": a.slug, "result": a.result, "method": a.method,
           "level": lvl, "level_name": LEVEL_NAME[lvl], "stability_days": stab,
           "fail_streak": streak, "status": status}
    if status == "blocked":
        res["advice"] = "连续 3 次失败：停止硬攻，转去补前置或换个时间再来，别继续磨"
    if lvl < 4:
        res["note"] = "只有 transfer（迁移到新情境）通过才算真掌握"
    out(res)


def cmd_set(a, con):
    fields = {}
    for kv in a.field:
        k, _, v = kv.partition("=")
        if k not in {"name", "domain", "aliases", "user_phrasing", "level",
                     "stability", "source", "status", "notes"}:
            out({"error": f"字段不可写: {k}"}); return
        fields[k] = int(v) if k == "level" else (float(v) if k == "stability" else v)
    if not fields:
        out({"error": "无字段"}); return
    con.execute(f"UPDATE atoms SET {','.join(k+'=?' for k in fields)}, updated=? WHERE slug=?",
                (*fields.values(), now(), a.slug))
    reindex(con, a.slug); con.commit()
    out({"ok": True, "slug": a.slug, "updated": list(fields)})


def cmd_edge(a, con):
    if a.action == "add":
        con.execute("INSERT OR IGNORE INTO edges(from_slug,to_slug,type) VALUES(?,?,?)",
                    (a.frm, a.to, a.type))
    else:
        con.execute("DELETE FROM edges WHERE from_slug=? AND to_slug=? AND type=?",
                    (a.frm, a.to, a.type))
    con.commit()
    out({"ok": True, "edge": f"{a.frm} -[{a.type}]-> {a.to}", "action": a.action})


def cmd_misconception(a, con):
    if a.action == "add":
        old = con.execute(
            "SELECT id,recurrence FROM misconceptions WHERE atom_slug=? AND wrong_belief=?",
            (a.atom, a.wrong)).fetchone()
        if old:
            con.execute("UPDATE misconceptions SET recurrence=recurrence+1, last_seen=?, "
                        "resolved=0 WHERE id=?", (now(), old["id"]))
            mid = old["id"]
        else:
            cur = con.execute(
                "INSERT INTO misconceptions(atom_slug,wrong_belief,correction,first_seen,last_seen) "
                "VALUES(?,?,?,?,?)", (a.atom, a.wrong, a.correction, now(), now()))
            mid = cur.lastrowid
        con.commit()
        out({"ok": True, "id": mid, "note": "复发次数高的误区，下次教到相关内容时主动提前拆解"})
    elif a.action == "resolve":
        con.execute("UPDATE misconceptions SET resolved=1 WHERE id=?", (a.id,))
        con.commit(); out({"ok": True, "id": a.id})
    else:
        rows = con.execute(
            "SELECT * FROM misconceptions WHERE (?='' OR atom_slug LIKE ?) "
            "AND (resolved=0 OR ?=1) ORDER BY recurrence DESC, last_seen DESC LIMIT 50",
            (a.atom or "", f"%{a.atom or ''}%", 1 if a.all else 0)).fetchall()
        out([dict(r) for r in rows])


def cmd_pref(a, con):
    """教学策略的三层进化。L1 直接生效；L2 需要 >=2 次同向证据才转正。

    门槛的意义：一次偶发反馈不该被固化成永久规则，否则规则集会互相打架、
    越滚越乱，最后没人敢信也没人敢删。
    """
    if a.action == "add":
        row = con.execute("SELECT * FROM teaching_prefs WHERE rule=? AND scope=?",
                          (a.rule, a.scope)).fetchone()
        ev = {"date": now(), "signal": a.evidence or ""}
        if row:
            evs = json.loads(row["evidence"] or "[]"); evs.append(ev)
            cnt = row["evidence_count"] + 1
            active = 1 if (row["layer"] == "L1" or cnt >= 2) else 0
            con.execute("UPDATE teaching_prefs SET evidence=?, evidence_count=?, active=? "
                        "WHERE id=?", (json.dumps(evs, ensure_ascii=False), cnt, active, row["id"]))
            pid, promoted = row["id"], bool(active and not row["active"])
        else:
            active = 1 if a.layer == "L1" else 0
            cur = con.execute(
                "INSERT INTO teaching_prefs(layer,scope,rule,polarity,evidence,evidence_count,"
                "active,created) VALUES(?,?,?,?,?,1,?,?)",
                (a.layer, a.scope, a.rule, a.polarity,
                 json.dumps([ev], ensure_ascii=False), active, now()))
            pid, promoted = cur.lastrowid, bool(active)
        con.commit()
        out({"ok": True, "id": pid, "active": bool(active), "promoted": promoted,
             "note": "L2 规则需 2 次同向证据才生效；候选规则会一直挂着直到被证实或过期"})
    elif a.action == "hit":
        con.execute("UPDATE teaching_prefs SET hit_count=hit_count+1, last_hit=? WHERE id=?",
                    (now(), a.id)); con.commit(); out({"ok": True, "id": a.id})
    elif a.action == "prune":
        # 90 天没被命中的生效规则降为候选，防止规则集无限膨胀
        stale = [r["id"] for r in con.execute(
            "SELECT id,last_hit,created FROM teaching_prefs WHERE active=1 AND layer='L2'")
            if (days_since(r["last_hit"] or r["created"]) or 0) > 90]
        for i in stale:
            con.execute("UPDATE teaching_prefs SET active=0 WHERE id=?", (i,))
        con.commit(); out({"demoted": stale, "count": len(stale)})
    else:
        rows = con.execute(
            "SELECT id,layer,scope,rule,polarity,evidence_count,active,hit_count,last_hit "
            "FROM teaching_prefs WHERE (?=1 OR active=1) ORDER BY active DESC, "
            "evidence_count DESC", (1 if a.all else 0,)).fetchall()
        out([dict(r) for r in rows])


def cmd_due(a, con):
    """按需复习模式用：用户说"我忘了/复习一下"时，列出确实在衰减的 atom。

    这个 skill 不做主动复习提醒，所以这条命令只在用户开口时才该被调用。
    """
    rows = con.execute(
        "SELECT * FROM atoms WHERE status='active' AND level>=2 "
        "AND (?='' OR domain LIKE ? OR slug LIKE ?)",
        (a.topic or "", f"%{a.topic or ''}%", f"%{a.topic or ''}%")).fetchall()
    items = [atom_view(r) for r in rows]
    items = [x for x in items if x["recall"] < DUE_THRESHOLD]
    items.sort(key=lambda x: x["recall"])
    out({"topic": a.topic, "count": len(items), "due": items[:a.limit],
         "hint": "复习先测后讲：先确认到底忘了多少，避免从头重讲浪费时间"})


def _phase_id(a):
    """phase 命令里 get/set/start/close 传的是数字 id。传错类型时给干净报错，
    而不是让 int() 抛 ValueError 打一屏 traceback 出来。"""
    try:
        return int(a.target)
    except (TypeError, ValueError):
        out({"error": f"这个动作需要 phase 的数字 id，收到的是 {a.target!r}。"
                      f"用 `phase list <goal_slug>` 查 id"})
        return None


def cmd_doctor(a, con):
    """数据完整性体检。表结构里没有外键约束（加 FK 要重建已有表，代价大于收益），
    所以引用完整性靠写入时校验 + 这个命令定期兜底。

    大改动之后、或者觉得探针/进度显示不对劲时跑一下。
    """
    issues = []
    for e in con.execute("SELECT from_slug,to_slug,type FROM edges"):
        for side in ("from_slug", "to_slug"):
            if not con.execute("SELECT 1 FROM atoms WHERE slug=?", (e[side],)).fetchone():
                issues.append({"kind": "dangling_edge", "detail": f"{e['from_slug']} -> {e['to_slug']}",
                               "missing_atom": e[side],
                               "fix": f"learndb.py add {e[side]} \"<名字>\"　或　"
                                      f"learndb.py edge rm {e['from_slug']} {e['to_slug']}"})
    for p in con.execute("SELECT slug,phase_id FROM projects"):
        if not con.execute("SELECT 1 FROM phases WHERE id=?", (p["phase_id"],)).fetchone():
            issues.append({"kind": "orphan_project", "detail": p["slug"],
                           "missing_phase_id": p["phase_id"],
                           "fix": f"learndb.py project set {p['slug']} --field status=skipped"})
    for pa in con.execute("SELECT project_slug,atom_slug FROM project_atoms"):
        if not con.execute("SELECT 1 FROM atoms WHERE slug=?", (pa["atom_slug"],)).fetchone():
            issues.append({"kind": "project_atom_missing", "detail":
                           f"{pa['project_slug']} -> {pa['atom_slug']}"})
    for ph in con.execute("SELECT id,goal_slug FROM phases"):
        if not con.execute("SELECT 1 FROM goals WHERE slug=?", (ph["goal_slug"],)).fetchone():
            issues.append({"kind": "orphan_phase", "detail": f"phase#{ph['id']}",
                           "missing_goal": ph["goal_slug"]})
    for r in con.execute("SELECT goal_slug,seq,COUNT(*) c FROM phases "
                         "GROUP BY goal_slug,seq HAVING c>1"):
        issues.append({"kind": "duplicate_phase_seq",
                       "detail": f"{r['goal_slug']} 的 seq={r['seq']} 有 {r['c']} 个阶段",
                       "fix": "phase set <id> --field seq=<新序号>；重复的 seq 会让 "
                              "phase close 推进到不确定的下一阶段"})
    for r in con.execute("SELECT slug,phase_id FROM atoms WHERE phase_id IS NOT NULL"):
        if not con.execute("SELECT 1 FROM phases WHERE id=?", (r["phase_id"],)).fetchone():
            issues.append({"kind": "atom_bad_phase", "detail": r["slug"]})
    for r in con.execute("SELECT COUNT(*) c FROM goals WHERE status='active'"):
        for g in con.execute("SELECT slug FROM goals WHERE status='active'"):
            n = con.execute("SELECT COUNT(*) c FROM phases WHERE goal_slug=? AND status='current'",
                            (g["slug"],)).fetchone()["c"]
            if n > 1:
                issues.append({"kind": "multiple_current_phases", "detail": g["slug"],
                               "fix": "phase set <id> --field status=upcoming"})
    n_atoms = con.execute("SELECT COUNT(*) c FROM atoms").fetchone()["c"]
    n_fts = con.execute("SELECT COUNT(*) c FROM atoms_fts").fetchone()["c"]
    if n_atoms != n_fts:
        issues.append({"kind": "fts_out_of_sync",
                       "detail": f"atoms={n_atoms} 但 atoms_fts={n_fts}",
                       "fix": "learndb.py doctor --repair-fts（中文模糊查找会漏结果）"})
    if a.repair_fts:
        reindex(con); con.commit()
        issues = [i for i in issues if i["kind"] != "fts_out_of_sync"]
        out({"repaired": "atoms_fts 已重建", "remaining_issues": issues, "count": len(issues)})
        return
    out({"ok": not issues, "issue_count": len(issues), "issues": issues,
         "schema_version": SCHEMA_VERSION,
         "hint": "全部为空说明数据完整性没问题" if not issues else
                 "按每条的 fix 处理；这些问题不会自己消失，而且大多是静默失败"})


def cmd_goal(a, con):
    """大目标：goal -> phase -> project 三层里，goal 是最上面那层。

    只有用户提出跨月的大目标（"学一年做出 X"）时才用得到这一族命令；
    日常单点学习完全不涉及 goal/phase/project，直接用 atom 相关命令。
    """
    if a.action == "add":
        if not a.slug or not a.name:
            out({"error": "用法: goal add <slug> <name> [--north-star ...] [--weekly-hours N]"}); return
        ts = now()
        con.execute(
            "INSERT INTO goals(slug,name,north_star,weekly_hours,notes,created,updated) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET name=excluded.name, "
            "north_star=COALESCE(excluded.north_star,goals.north_star), "
            "weekly_hours=COALESCE(excluded.weekly_hours,goals.weekly_hours), updated=excluded.updated",
            (a.slug, a.name, a.north_star, a.weekly_hours, a.notes, ts, ts))
        con.commit()
        out({"ok": True, "slug": a.slug,
             "hint": "接下来用 phase add 建里程碑；只精细展开当前阶段+下一阶段预览，"
                     "更远的阶段先只给粗粒度名字和验收标准，别急着展开到 atom/project"})
    elif a.action == "list":
        rows = con.execute("SELECT * FROM goals WHERE (?='' OR status=?) ORDER BY created",
                           (a.status or "", a.status or "")).fetchall()
        out([dict(r) for r in rows])
    elif a.action == "get":
        g = con.execute("SELECT * FROM goals WHERE slug=?", (a.slug,)).fetchone()
        if not g:
            out({"error": "not found", "slug": a.slug}); return
        d = dict(g)
        d["phases"] = []
        for p in con.execute("SELECT * FROM phases WHERE goal_slug=? ORDER BY seq", (a.slug,)):
            pd = dict(p); pd["progress"] = phase_progress(con, p["id"]); d["phases"].append(pd)
        out(d)
    elif a.action == "set":
        fields = {}
        for kv in (a.field or []):
            k, _, v = kv.partition("=")
            if k not in {"name", "north_star", "weekly_hours", "notes", "status"}:
                out({"error": f"字段不可写: {k}"}); return
            fields[k] = float(v) if k == "weekly_hours" else v
        if not fields:
            out({"error": "无字段"}); return
        con.execute(f"UPDATE goals SET {','.join(k+'=?' for k in fields)}, updated=? WHERE slug=?",
                    (*fields.values(), now(), a.slug))
        con.commit(); out({"ok": True, "slug": a.slug, "updated": list(fields)})
    elif a.action == "roadmap":
        g = con.execute("SELECT * FROM goals WHERE slug=?", (a.slug,)).fetchone()
        if not g:
            out({"error": "not found", "slug": a.slug}); return
        tree = {"goal": dict(g), "phases": []}
        for p in con.execute("SELECT * FROM phases WHERE goal_slug=? ORDER BY seq", (a.slug,)):
            pd = dict(p)
            pd["progress"] = phase_progress(con, p["id"])
            pd["projects"] = [dict(x) for x in con.execute(
                "SELECT slug,name,status,est_effort FROM projects WHERE phase_id=? ORDER BY created",
                (p["id"],))]
            tree["phases"].append(pd)
        out(tree)
    else:
        out({"error": "unknown action"})


def cmd_phase(a, con):
    """里程碑。用验收标准（objective）定义边界，不是用日期——`est_weeks` 只是参考,
    真正的时间感靠 `close` 时的复盘滚动校准出来。
    """
    if a.action == "add":
        if not a.target or not a.name or a.seq is None:
            out({"error": "用法: phase add <goal_slug> <name> --seq N "
                          "[--objective ...] [--est-weeks N] [--detail coarse|detailed]"}); return
        if not con.execute("SELECT 1 FROM goals WHERE slug=?", (a.target,)).fetchone():
            out({"error": f"大目标 {a.target!r} 不存在，先跑 goal add"}); return
        dup = con.execute("SELECT id,name FROM phases WHERE goal_slug=? AND seq=?",
                          (a.target, a.seq)).fetchone()
        if dup:
            # seq 重复会让 phase close 找 seq+1 时推进到不确定的阶段
            out({"error": f"seq={a.seq} 已被阶段 #{dup['id']}「{dup['name']}」占用。"
                          f"换一个序号，或先 phase set {dup['id']} --field seq=<新序号>"}); return
        cur = con.execute(
            "INSERT INTO phases(goal_slug,seq,name,objective,detail_level,est_weeks,status,created) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (a.target, a.seq, a.name, a.objective, a.detail or "coarse", a.est_weeks, "upcoming", now()))
        con.commit()
        out({"ok": True, "id": cur.lastrowid, "seq": a.seq,
             "hint": "第一个阶段建完后记得跑 phase start，把它设为当前阶段"})
    elif a.action == "list":
        items = []
        for r in con.execute("SELECT * FROM phases WHERE goal_slug=? ORDER BY seq", (a.target,)):
            d = dict(r); d["progress"] = phase_progress(con, r["id"]); items.append(d)
        out(items)
    elif a.action == "get":
        pid = _phase_id(a)
        if pid is None: return
        r = con.execute("SELECT * FROM phases WHERE id=?", (pid,)).fetchone()
        if not r:
            out({"error": "not found"}); return
        d = dict(r); d["progress"] = phase_progress(con, pid)
        d["atoms"] = [dict(x) for x in con.execute(
            "SELECT slug,name,level,status FROM atoms WHERE phase_id=?", (pid,))]
        d["projects"] = [dict(x) for x in con.execute(
            "SELECT slug,name,status FROM projects WHERE phase_id=?", (pid,))]
        out(d)
    elif a.action == "set":
        pid = _phase_id(a)
        if pid is None: return
        fields = {}
        for kv in (a.field or []):
            k, _, v = kv.partition("=")
            if k not in {"name", "objective", "detail_level", "est_weeks", "status"}:
                out({"error": f"字段不可写: {k}"}); return
            fields[k] = float(v) if k == "est_weeks" else v
        if not fields:
            out({"error": "无字段"}); return
        con.execute(f"UPDATE phases SET {','.join(k+'=?' for k in fields)} WHERE id=?",
                    (*fields.values(), pid))
        con.commit(); out({"ok": True, "id": pid, "updated": list(fields)})
    elif a.action == "start":
        pid = _phase_id(a)
        if pid is None: return
        if not con.execute("SELECT 1 FROM phases WHERE id=?", (pid,)).fetchone():
            out({"error": f"phase #{pid} 不存在"}); return
        con.execute("UPDATE phases SET status='current', detail_level='detailed', "
                    "started=COALESCE(started,?) WHERE id=?", (now(), pid))
        nxt = con.execute(
            "SELECT id FROM phases WHERE goal_slug=(SELECT goal_slug FROM phases WHERE id=?) "
            "AND seq=(SELECT seq FROM phases WHERE id=?)+1", (pid, pid)).fetchone()
        if nxt:
            con.execute("UPDATE phases SET detail_level='detailed' WHERE id=? AND detail_level='coarse'",
                        (nxt["id"],))
        con.commit()
        out({"ok": True, "id": pid, "next_phase_previewed": bool(nxt),
             "hint": "下一阶段已可以开始展开到 atom/project 级别；更远的阶段继续保持粗粒度"})
    elif a.action == "close":
        pid = _phase_id(a)
        if pid is None: return
        r = con.execute("SELECT * FROM phases WHERE id=?", (pid,)).fetchone()
        if not r:
            out({"error": "not found"}); return
        started = r["started"] or r["created"]
        actual_weeks = round((days_since(started) or 0) / 7, 2)
        con.execute("UPDATE phases SET status='done', ended=?, actual_weeks=?, retrospective=? WHERE id=?",
                    (now(), actual_weeks, a.retro, pid))
        ratio, recalibrated = None, []
        if r["est_weeks"] and r["est_weeks"] > 0 and actual_weeks > 0:
            ratio = round(max(0.5, min(2.0, actual_weeks / r["est_weeks"])), 2)
            for rp in con.execute(
                "SELECT id, est_weeks FROM phases WHERE goal_slug=? AND seq>? "
                "AND status IN ('upcoming','current') AND est_weeks IS NOT NULL",
                    (r["goal_slug"], r["seq"])):
                new_est = round(rp["est_weeks"] * ratio, 1)
                con.execute("UPDATE phases SET est_weeks=? WHERE id=?", (new_est, rp["id"]))
                recalibrated.append({"id": rp["id"], "old_est_weeks": rp["est_weeks"], "new_est_weeks": new_est})
        nxt = con.execute("SELECT id, name FROM phases WHERE goal_slug=? AND seq=?",
                          (r["goal_slug"], r["seq"] + 1)).fetchone()
        if nxt:
            con.execute("UPDATE phases SET status='current', detail_level='detailed', "
                        "started=COALESCE(started,?) WHERE id=?", (now(), nxt["id"]))
            nxt2 = con.execute("SELECT id FROM phases WHERE goal_slug=? AND seq=?",
                              (r["goal_slug"], r["seq"] + 2)).fetchone()
            if nxt2:
                con.execute("UPDATE phases SET detail_level='detailed' WHERE id=? AND detail_level='coarse'",
                            (nxt2["id"],))
        con.commit()
        out({"ok": True, "id": pid, "actual_weeks": actual_weeks, "est_weeks_was": r["est_weeks"],
             "velocity_ratio": ratio, "recalibrated_future_phases": recalibrated,
             "advanced_to": dict(nxt) if nxt else None,
             "hint": "把 recalibrated_future_phases 里的新估计如实告诉用户，说明只是参考不是承诺"})
    else:
        out({"error": "unknown action"})


def cmd_project(a, con):
    """阶段内的巩固项目。核心规则写在 --scaffold / --user-scope 两个字段里：
    没有这两个字段的项目不算设计完整——参见 SKILL.md 的脚手架透明约束。
    """
    if a.action == "add":
        if not a.arg1 or not a.arg2 or not a.arg3:
            out({"error": "用法: project add <phase_id> <slug> <name> "
                          "--purpose ... --scaffold ... --user-scope ... [--atom slug]*"}); return
        try:
            phase_id = int(a.arg1)
        except (TypeError, ValueError):
            out({"error": f"phase_id 必须是数字，收到 {a.arg1!r}；用 phase list <goal_slug> 查"}); return
        if not con.execute("SELECT 1 FROM phases WHERE id=?", (phase_id,)).fetchone():
            # 挂到不存在的阶段 = 这个项目永远不会出现在任何 roadmap 里
            out({"error": f"phase #{phase_id} 不存在，先 phase add"}); return
        slug, name = a.arg2, a.arg3
        ts = now()
        con.execute(
            "INSERT INTO projects(slug,phase_id,name,purpose,scaffold_provided,user_scope,est_effort,"
            "status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO UPDATE SET "
            "name=excluded.name, updated=excluded.updated",
            (slug, phase_id, name, a.purpose, a.scaffold, a.user_scope, a.effort, "planned", ts, ts))
        for atom_slug in (a.atom or []):
            con.execute("INSERT OR IGNORE INTO project_atoms(project_slug,atom_slug) VALUES(?,?)",
                        (slug, atom_slug))
        con.commit()
        missing = [n for flag, n in
                   [(a.scaffold, "scaffold_provided（帮用户写好的部分及为什么）"),
                    (a.user_scope, "user_scope（用户自己要写/设计的那一小块）")] if not flag]
        res = {"ok": True, "slug": slug, "atoms": a.atom or []}
        if missing:
            res["warning"] = "缺少脚手架透明字段，创建前必须想清楚: " + "; ".join(missing)
        out(res)
    elif a.action == "list":
        try:
            pid = int(a.arg1)
        except (TypeError, ValueError):
            out({"error": f"phase_id 必须是数字，收到 {a.arg1!r}"}); return
        out([dict(r) for r in con.execute(
            "SELECT slug,name,status,est_effort FROM projects WHERE phase_id=? ORDER BY created",
            (pid,))])
    elif a.action == "get":
        r = con.execute("SELECT * FROM projects WHERE slug=?", (a.arg1,)).fetchone()
        if not r:
            out({"error": "not found"}); return
        d = dict(r)
        d["atoms"] = [x["atom_slug"] for x in con.execute(
            "SELECT atom_slug FROM project_atoms WHERE project_slug=?", (a.arg1,))]
        out(d)
    elif a.action == "set":
        fields = {}
        for kv in (a.field or []):
            k, _, v = kv.partition("=")
            if k not in {"name", "purpose", "scaffold_provided", "user_scope", "est_effort",
                         "status", "artifact_path"}:
                out({"error": f"字段不可写: {k}"}); return
            fields[k] = v
        if not fields:
            out({"error": "无字段"}); return
        con.execute(f"UPDATE projects SET {','.join(k+'=?' for k in fields)}, updated=? WHERE slug=?",
                    (*fields.values(), now(), a.arg1))
        con.commit(); out({"ok": True, "slug": a.arg1, "updated": list(fields)})
    elif a.action == "done":
        slug = a.arg1
        r = con.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
        if not r:
            out({"error": "not found"}); return
        con.execute("UPDATE projects SET status='done', completed=?, updated=? WHERE slug=?",
                    (now(), now(), slug))
        con.commit()
        atoms = [x["atom_slug"] for x in con.execute(
            "SELECT atom_slug FROM project_atoms WHERE project_slug=?", (slug,))]
        out({"ok": True, "slug": slug, "linked_atoms": atoms,
             "hint": "项目完成不等于自动掌握——按用户实际写出的那部分表现，对上面每个 atom "
                     "跑一次 verify --method transfer，过了才算真到 level 4"})
    else:
        out({"error": "unknown action"})


def cmd_session(a, con):
    if a.action == "start":
        cur = con.execute("INSERT INTO sessions(started,goal) VALUES(?,?)", (now(), a.goal))
        con.commit(); out({"session_id": cur.lastrowid, "started": now()})
    else:
        sid = a.id or con.execute("SELECT MAX(id) FROM sessions").fetchone()[0]
        con.execute("UPDATE sessions SET ended=?, atoms_touched=?, summary=?, signals=? WHERE id=?",
                    (now(), ",".join(a.atoms or []), a.summary, a.signals, sid))
        con.commit(); out({"ok": True, "session_id": sid})


def cmd_export(a, con):
    """导出人类可读视图。SQLite 是真源，这个 md 是给用户翻阅和 git 备份用的。"""
    path = a.path or os.path.join(os.path.dirname(DB_PATH), "knowledge.md")
    lines = [f"# 知识库快照\n", f"生成于 {now()}\n"]
    prof = {r["key"]: r["value"] for r in con.execute("SELECT * FROM profile")}
    if prof:
        lines.append("## 学习者档案\n")
        lines += [f"- **{k}**: {v}" for k, v in prof.items()]
        lines.append("")
    domains = [r[0] or "(未分类)" for r in con.execute(
        "SELECT DISTINCT domain FROM atoms ORDER BY domain")]
    for d in domains:
        rows = con.execute(
            "SELECT * FROM atoms WHERE COALESCE(domain,'(未分类)')=? ORDER BY level DESC, name",
            (d,)).fetchall()
        lines.append(f"\n## {d}\n")
        lines.append("| 知识点 | 掌握度 | 估算回忆概率 | 上次验证 | 状态 |")
        lines.append("|---|---|---|---|---|")
        for r in rows:
            v = atom_view(r)
            rec = v["recall"] if r["level"] >= 2 else "—"
            lines.append(f"| {r['name']} (`{r['slug']}`) | {v['level_name']} | "
                         f"{rec} | {(r['last_verified'] or '—')[:10]} | {r['status']} |")
    misc = con.execute(
        "SELECT * FROM misconceptions WHERE resolved=0 ORDER BY recurrence DESC").fetchall()
    if misc:
        lines.append("\n## 未解决的理解误区\n")
        for m in misc:
            lines.append(f"- **{m['atom_slug'] or '通用'}** — 误：{m['wrong_belief']}　→　"
                         f"正：{m['correction']}（出现 {m['recurrence']} 次）")
    prefs = con.execute("SELECT * FROM teaching_prefs WHERE active=1").fetchall()
    if prefs:
        lines.append("\n## 生效中的教学策略\n")
        for p in prefs:
            mark = "✅" if p["polarity"] == "works" else "🚫"
            lines.append(f"- {mark} [{p['scope']}] {p['rule']}（证据 {p['evidence_count']} 次）")

    # 路线图必须进快照——它是跨月计划的可读视图和 git 备份路径。
    # 早期版本这里完全没有 goal/phase/project，导出的 knowledge.md 看不出
    # 用户正在推进什么大目标，等于备份漏了一整块。
    goals = con.execute("SELECT * FROM goals ORDER BY status, created").fetchall()
    if goals:
        lines.append("\n## 学习路线图\n")
        for g in goals:
            lines.append(f"\n### {g['name']} (`{g['slug']}`) — {g['status']}\n")
            if g["north_star"]:
                lines.append(f"**目标（成功长什么样）**：{g['north_star']}\n")
            if g["weekly_hours"]:
                lines.append(f"**每周可投入**：{g['weekly_hours']} 小时\n")
            phs = con.execute("SELECT * FROM phases WHERE goal_slug=? ORDER BY seq",
                              (g["slug"],)).fetchall()
            if not phs:
                continue
            lines.append("| # | 阶段 | 验收标准 | 状态 | 估计/实际(周) | 进度 |")
            lines.append("|---|---|---|---|---|---|")
            for ph in phs:
                pr = phase_progress(con, ph["id"])
                mark = {"current": "▶", "done": "✅", "skipped": "⏭"}.get(ph["status"], "")
                wk = f"{ph['est_weeks'] or '—'} / {ph['actual_weeks'] or '—'}"
                prog = (f"知识点 {pr['atoms_transfer_proven']}/{pr['atoms_total']} 掌握，"
                        f"项目 {pr['projects_done']}/{pr['projects_total']}")
                detail = "" if ph["detail_level"] == "detailed" else "（未展开）"
                lines.append(f"| {ph['seq']} | {mark} {ph['name']}{detail} | "
                             f"{ph['objective'] or '—'} | {ph['status']} | {wk} | {prog} |")
            for ph in phs:
                projs = con.execute("SELECT * FROM projects WHERE phase_id=? ORDER BY created",
                                    (ph["id"],)).fetchall()
                if not projs:
                    continue
                lines.append(f"\n**阶段 {ph['seq']}「{ph['name']}」的项目**\n")
                for pj in projs:
                    box = "x" if pj["status"] == "done" else " "
                    lines.append(f"- [{box}] **{pj['name']}** (`{pj['slug']}`)"
                                 f"{'　— ' + pj['est_effort'] if pj['est_effort'] else ''}")
                    if pj["user_scope"]:
                        lines.append(f"  - 你要写的部分：{pj['user_scope']}")
                    if pj["scaffold_provided"]:
                        lines.append(f"  - 已提供的脚手架：{pj['scaffold_provided']}")
            if any(ph["retrospective"] for ph in phs):
                lines.append("\n**阶段复盘**\n")
                for ph in phs:
                    if ph["retrospective"]:
                        lines.append(f"- 阶段 {ph['seq']}「{ph['name']}」：{ph['retrospective']}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    out({"ok": True, "path": path})


def cmd_backup(a, con):
    con.commit()
    dst = os.path.join(os.path.dirname(DB_PATH), "backups")
    os.makedirs(dst, exist_ok=True)
    p = os.path.join(dst, f"learn-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
    shutil.copy2(DB_PATH, p)
    keep = sorted(os.listdir(dst))[-20:]
    for f in os.listdir(dst):
        if f not in keep:
            os.remove(os.path.join(dst, f))
    out({"ok": True, "backup": p})


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="创建/升级数据库").set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", help="数据完整性体检（大改动后、或觉得探针/进度不对劲时跑）")
    p.add_argument("--repair-fts", action="store_true", dest="repair_fts",
                   help="重建中文模糊查找用的 FTS 索引")
    p.set_defaults(fn=cmd_doctor)
    sub.add_parser("status", help="总览：档案、规模、领域、未解决误区").set_defaults(fn=cmd_status)
    sub.add_parser("backup", help="快照数据库（写入前建议先跑）").set_defaults(fn=cmd_backup)

    p = sub.add_parser("profile", help="读写学习者档案")
    p.add_argument("--set", nargs="*", metavar="KEY=VALUE")
    p.set_defaults(fn=cmd_profile)

    p = sub.add_parser("find", help="模糊定位 atom（支持中文，按需复习入口）")
    p.add_argument("query"); p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("get", help="单个 atom 详情 + 前置 + 误区")
    p.add_argument("slug"); p.set_defaults(fn=cmd_get)

    p = sub.add_parser("probe", help="教某 atom 前，列出值得探针的直接前置")
    p.add_argument("slug"); p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("plan", help="目标 atom 的全部前置闭包（拓扑序）")
    p.add_argument("slug"); p.add_argument("--gaps-only", action="store_true")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("context", help="按主题切一个精简上下文包（教学开始时用）")
    p.add_argument("topic"); p.add_argument("--limit", type=int, default=25)
    p.set_defaults(fn=cmd_context)

    p = sub.add_parser("add", help="新增/更新 atom")
    p.add_argument("slug"); p.add_argument("name")
    p.add_argument("--domain"); p.add_argument("--alias", action="append")
    p.add_argument("--prereq", action="append"); p.add_argument("--related", action="append")
    p.add_argument("--notes"); p.add_argument("--source")
    p.add_argument("--phase", type=int, help="挂到某个大目标的 phase id（goal 模式下用）")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("verify", help="记一次检验结果（教学循环的记账动作）")
    p.add_argument("slug")
    p.add_argument("--result", choices=["pass", "fail"], required=True)
    p.add_argument("--method", choices=list(METHOD_LEVEL), required=True)
    p.add_argument("--phrasing", help="用户复述时的原话，后续教学复用")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("set", help="直接改 atom 字段")
    p.add_argument("slug"); p.add_argument("field", nargs="+", metavar="KEY=VALUE")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("edge", help="维护依赖图")
    p.add_argument("action", choices=["add", "rm"])
    p.add_argument("frm"); p.add_argument("to")
    p.add_argument("--type", default="prereq", choices=["prereq", "related"])
    p.set_defaults(fn=cmd_edge)

    p = sub.add_parser("misconception", help="记录/查询用户的具体错误理解")
    p.add_argument("action", choices=["add", "list", "resolve"])
    p.add_argument("--atom"); p.add_argument("--wrong"); p.add_argument("--correction")
    p.add_argument("--id", type=int); p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_misconception)

    p = sub.add_parser("pref", help="教学策略进化（L1 自动 / L2 需两次证据）")
    p.add_argument("action", choices=["add", "list", "hit", "prune"])
    p.add_argument("--rule"); p.add_argument("--scope", default="global")
    p.add_argument("--layer", default="L2", choices=["L1", "L2"])
    p.add_argument("--polarity", default="works", choices=["works", "fails"])
    p.add_argument("--evidence"); p.add_argument("--id", type=int)
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_pref)

    p = sub.add_parser("due", help="列出正在衰减的 atom（仅用户主动要求复习时用）")
    p.add_argument("--topic"); p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_due)

    p = sub.add_parser("session", help="会话记账")
    p.add_argument("action", choices=["start", "close"])
    p.add_argument("--goal"); p.add_argument("--id", type=int)
    p.add_argument("--atoms", nargs="*"); p.add_argument("--summary")
    p.add_argument("--signals", help="本次观察到的教学信号，供进化用")
    p.set_defaults(fn=cmd_session)

    p = sub.add_parser("export", help="导出 knowledge.md 人类可读视图")
    p.add_argument("--path"); p.set_defaults(fn=cmd_export)

    p = sub.add_parser("goal", help="大目标：跨月学习目标的增删查、生成路线图")
    p.add_argument("action", choices=["add", "list", "get", "set", "roadmap"])
    p.add_argument("slug", nargs="?"); p.add_argument("name", nargs="?")
    p.add_argument("--north-star", dest="north_star", help="第0步澄清后：成功具体长什么样")
    p.add_argument("--weekly-hours", type=float)
    p.add_argument("--notes"); p.add_argument("--status")
    p.add_argument("--field", action="append", metavar="KEY=VALUE", help="用于 set 动作")
    p.set_defaults(fn=cmd_goal)

    p = sub.add_parser("phase", help="大目标下的里程碑（近详远粗 + 结束时滚动校准）")
    p.add_argument("action", choices=["add", "list", "get", "set", "start", "close"])
    p.add_argument("target", nargs="?", help="add/list 传 goal_slug；get/set/start/close 传 phase id")
    p.add_argument("name", nargs="?", help="仅 add 用")
    p.add_argument("--seq", type=int); p.add_argument("--objective")
    p.add_argument("--est-weeks", type=float, dest="est_weeks")
    p.add_argument("--detail", choices=["coarse", "detailed"])
    p.add_argument("--retro", help="close 时的复盘笔记")
    p.add_argument("--field", action="append", metavar="KEY=VALUE")
    p.set_defaults(fn=cmd_phase)

    p = sub.add_parser("project", help="阶段内的巩固项目（必须写清楚脚手架 vs 用户自己写的部分）")
    p.add_argument("action", choices=["add", "list", "get", "set", "done"])
    p.add_argument("arg1", nargs="?", help="add: phase_id / list: phase_id / get,set,done: slug")
    p.add_argument("arg2", nargs="?", help="仅 add 用：slug")
    p.add_argument("arg3", nargs="?", help="仅 add 用：name")
    p.add_argument("--purpose"); p.add_argument("--scaffold"); p.add_argument("--user-scope", dest="user_scope")
    p.add_argument("--effort", help="粗略工作量，如 '1-2 次学习会话'")
    p.add_argument("--atom", action="append", help="这个项目巩固/应用的 atom，可多次传")
    p.add_argument("--field", action="append", metavar="KEY=VALUE")
    p.set_defaults(fn=cmd_project)

    a = ap.parse_args()
    con = connect()
    if a.cmd != "init":
        try:
            con.execute("SELECT 1 FROM atoms LIMIT 1")
        except sqlite3.OperationalError:
            out({"error": "数据库未初始化，先运行: python3 learndb.py init"}); sys.exit(1)
        migrate(con)  # 老库升级到新 schema（比如补上 goals/phases/projects 三张表）
    a.fn(a, con)
    con.close()


if __name__ == "__main__":
    main()
