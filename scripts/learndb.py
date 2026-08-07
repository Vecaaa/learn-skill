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
# 这两个阈值必须和 recall_prob 的曲线形状一起理解，不能单独调。
# 当前是 FSRS 的乘方曲线 R(t,S) = (1 + t/(9S))^-1，代进去解出触发时刻：
#   R=0.9 → t = S        （探针：正好是 stability 的定义，"降到90%所需天数"）
#   R=0.8 → t = 2.25S     （复习列表：比探针晚一倍多）
# 探针比 due 更早触发是刻意的——代价不对称：多探一题成本十秒，漏探的代价是
# 讲二十分钟对方一脸茫然。
#
# ⚠️ 换 recall_prob 的曲线时**必须重算这两个值**。2026-08-05 从指数曲线
# exp(-t/S) 换到乘方曲线时漏了这一步，这两个值还是按指数曲线校准的 0.7/0.6，
# 结果探针触发时刻整整推迟了 10.8 倍（S=2.13 的 atom 从"0.8 天后探"变成
# "8.2 天后探"），核心机制静默失准。
PROBE_THRESHOLD = 0.9   # 估算回忆概率低于此值 → 教学前值得探针
DUE_THRESHOLD = 0.8     # 按需复习模式里认定"已经开始忘"的线
# 从未验证过的 atom，recall_prob 返回 0.0——那表示"没有数据"，不是
# "预期已经忘光"。但 update_stability 把低 r 读作"通过得很意外→记得比预期牢
# →大幅加 stability"，两种含义混在一起会让首次通过的 stability 被系统性高估
# （实测 2.13 天 vs 合理的 1.28 天）。首次验证时改用这个值代入。
FIRST_VERIFY_RECALL = 0.9
DISTILL_MIN_PREFS = 3   # 同一 concept_type 下积累到几条生效 pref，就该提炼成画像
DISTILL_MIN_MISC = 2    # 同一 error_type 下积累到几条误区，就该提炼成画像
INSIGHT_PREFIX = "insight:"  # profile 里认知画像条目的命名约定，distill 靠它检测
MAX_INSIGHTS_IN_GUIDANCE = 8  # probe 每次都带画像，超过这个数就该合并而不是继续堆
# 一条教学指导累计 miss 到这个数、且 miss 多于 hit，就判定它没起作用：
# pref 自动降回候选，insight 由 distill 报出来要求复核。
# 定 3 而不是 2：偶尔一次失败可能是内容本身难，不该急着怪教法。
PREF_MISS_LIMIT = 3
INSIGHT_REVIEW_DAYS = 60  # 画像超过这么久没复核过，distill 提醒重新审视


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
    """FSRS 启发的乘方遗忘曲线：R(t,S) = (1 + t/(9S))^-1。

    S 被重新定义为"记忆概率降到 90% 所需的天数"（代入 t=S 验证：
    R = (1+1/9)^-1 = 0.9，这是 FSRS 的标准约定，S 从此是一个有具体
    含义的量，不再是任意的衰减常数）。乘方曲线比旧版的指数曲线
    exp(-t/S) 更贴近真实遗忘数据——真实遗忘在早期下降更快、长期尾部
    更平缓，指数曲线两段都会系统性偏离（这是 FSRS 相比更早的 SM-2
    指数模型的核心改进点）。

    这不是完整的 FSRS——真正的 FSRS 有 17~21 个在数百万条 Anki 复习
    记录上训练出的参数，个人单用户场景没有那个数据量去验证，硬套
    反而可能不准。这里保留的是它的核心洞察（乘方曲线 + S 的语义），
    常数是保守选择而非拟合出来的，见 `update_stability()`。
    """
    t = days_since(last_verified)
    if t is None:
        return 0.0
    s = max(0.1, float(stability or 1.0))
    return round((1 + t / (9 * s)) ** -1, 3)


def update_difficulty(d, passed, ml):
    """检验后调整"难度"（1~10，越大越难），FSRS 里這是独立于 stability 的
    第二个状态量，用来让 stability 的增长/衰减幅度感知"这个 atom 本身多难"。

    简化版，非 FSRS 原始拟合参数：通过时难度下调，检验方法越硬（transfer
    比 recall 更能说明"其实没那么难"）下调越多；失败时难度上调，方法越软
    （连 recall 都过不了更能说明"真的难"）上调越多。
    """
    d = float(d or 5.0)
    if passed:
        d -= 0.3 + 0.15 * ml
    else:
        d += 0.6 - 0.1 * ml
    return round(min(10.0, max(1.0, d)), 2)


def update_stability(s_old, difficulty, passed, ml, r_now):
    """FSRS 的核心洞察：**用一次检验的"意外程度"和"这个 atom 多难"共同决定
    stability 该涨多少/跌多少**，而不是只看"这次结果+检验强度"两个变量
    （旧版 `cmd_verify` 只用了后者）。

    - 通过时：`r_now` 是这次检验发生时估算的回忆概率——如果本来已经算出
      "大概率快忘了"（r_now 低）结果还是通过了，说明记得比预期牢，
      stability 涨得应该更多；反之如果 r_now 本来就很高，通过是意料之中，
      信息量小，涨幅应该小。同时难度越低（d 越小）天然涨得越多。
    - 失败时：难度越高、失败前 r_now 越低（本来就没抱太大期望），惩罚
      相对轻一些；难度低、r_now 高（"这么简单/这么新鲜居然还错了"）
      惩罚更重。

    同样是简化版、非 FSRS 原始训练参数，常数是保守选择。
    """
    s_old = max(0.1, float(s_old or 1.0))
    d = float(difficulty or 5.0)
    r = min(1.0, max(0.0, r_now))
    if passed:
        growth = 1.0 + (11 - d) / 10 * (1.2 - r) * (0.5 + 0.3 * ml)
        s_new = s_old * growth
    else:
        penalty = 0.15 + 0.25 * (1 - d / 10) + 0.1 * r - 0.03 * ml
        s_new = s_old * max(0.15, penalty)
    return round(min(365.0, max(0.5, s_new)), 2)


# schema 版本。加表/加列时 +1，并在 migrate() 里补上对应的升级动作。
# migrate() 靠它走快速路径，避免每条命令都重跑一遍全量 DDL。
SCHEMA_VERSION = 7


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
    stability REAL DEFAULT 1.0,  -- 遗忘曲线的 S（天），FSRS 语义：降到90%所需天数
    difficulty REAL DEFAULT 5.0, -- 1~10，越大越难，见 update_difficulty()
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
    error_type TEXT,             -- 跨领域错误模式：quantifier/type-mismatch/causal-inversion 等
    first_seen TEXT, last_seen TEXT,
    recurrence INTEGER DEFAULT 1,
    resolved INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_misc_atom ON misconceptions(atom_slug);

CREATE TABLE IF NOT EXISTS teaching_prefs (
    id INTEGER PRIMARY KEY,
    layer TEXT DEFAULT 'L2',     -- L1=用户模型(自动生效) L2=教学策略(需证据)
    scope TEXT DEFAULT 'global', -- global / domain:math / atom:closure
    concept_type TEXT,           -- 跨领域维度：notation/comparison/process/quantifier 等，独立于 scope
    rule TEXT NOT NULL,
    polarity TEXT DEFAULT 'works',-- works / fails，失败教法同样要记
    evidence TEXT,               -- JSON 数组
    evidence_count INTEGER DEFAULT 1,
    active INTEGER DEFAULT 0,
    hit_count INTEGER DEFAULT 0,
    last_hit TEXT,
    created TEXT,
    retired INTEGER DEFAULT 0,   -- 规则被证伪/被更精确的规则取代时置 1，而非删除（保留审计轨迹）
    retired_reason TEXT,
    retired_at TEXT
);

-- 教学指导的有效性反馈。pref 和 insight 用同一张表，因为"这条指导管不管用"
-- 的判定逻辑对两者完全相同——分开做会变成同一概念的两处实现。
--
-- 为什么需要它：在此之前所有证据都是单向的（evidence_count / hit_count 只增
-- 不减，insight 连计数都没有），一条错误的教学策略没有任何下台通道。更糟的是
-- 归因错位：照错规则教 → 用户连错 → 系统把失败 100% 记到 atom 的 difficulty
-- 上（"这知识点真难"），规则本身 evidence_count 毫发无损，继续被反复使用。
-- 错误的教法会伪装成"内容难"，而且会自我强化。
CREATE TABLE IF NOT EXISTS guidance_feedback (
    kind TEXT NOT NULL,          -- 'pref'（值为 teaching_prefs.id）/ 'insight'（值为 type）
    ref TEXT NOT NULL,
    hits INTEGER DEFAULT 0,      -- 用了它、且随后的检验通过
    misses INTEGER DEFAULT 0,    -- 用了它、但随后的检验失败
    last_hit TEXT,
    last_miss TEXT,
    PRIMARY KEY (kind, ref)
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
    if "difficulty" not in cols:
        con.execute("ALTER TABLE atoms ADD COLUMN difficulty REAL DEFAULT 5.0")
    pref_cols = {r[1] for r in con.execute("PRAGMA table_info(teaching_prefs)")}
    if "retired" not in pref_cols:
        con.execute("ALTER TABLE teaching_prefs ADD COLUMN retired INTEGER DEFAULT 0")
        con.execute("ALTER TABLE teaching_prefs ADD COLUMN retired_reason TEXT")
        con.execute("ALTER TABLE teaching_prefs ADD COLUMN retired_at TEXT")
    if "concept_type" not in pref_cols:
        con.execute("ALTER TABLE teaching_prefs ADD COLUMN concept_type TEXT")
    misc_cols = {r[1] for r in con.execute("PRAGMA table_info(misconceptions)")}
    if misc_cols and "error_type" not in misc_cols:
        con.execute("ALTER TABLE misconceptions ADD COLUMN error_type TEXT")
    con.executescript(SCHEMA)  # 先建好 guidance_feedback 再往里搬历史数据
    # hit_count/last_hit 从 teaching_prefs 搬到 guidance_feedback，之后只认新表。
    # 留着旧列不删（SQLite 删列要重建表，代价大于收益），但代码不再读写它们。
    if pref_cols and "hit_count" in pref_cols:
        for r in con.execute("SELECT id,hit_count,last_hit FROM teaching_prefs "
                             "WHERE hit_count>0").fetchall():
            con.execute(
                "INSERT INTO guidance_feedback(kind,ref,hits,last_hit) VALUES('pref',?,?,?) "
                "ON CONFLICT(kind,ref) DO NOTHING",
                (str(r["id"]), r["hit_count"], r["last_hit"]))
        con.execute("UPDATE teaching_prefs SET hit_count=0")
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
    """安全取列：老库/局部 SELECT 可能没有这一列，行本身也可能是 None。"""
    try:
        return r[name]
    except (IndexError, KeyError, TypeError):
        return default


# 引导量档位：按该 domain 下"能应用及以上"的 atom 绝对数量分档。
# 用绝对数而不是比例——路线刚建好时大批 atom 还是 level 0，比例会把
# 一个其实已经很熟的领域误判成新手。
GUIDANCE_TIERS = [
    (3,  "novice",     "完整引导：最小可跑实例 → 为什么这样 → 一般化规则 → 边界/反例，四步都给"),
    (7,  "developing", "压缩引导：实例和一般化规则各留一句，省掉铺垫，直接进这个领域的惯用记号"),
    (99, "proficient", "最小引导：直接给形式化陈述/原文，只标注与已知内容的差异点；"
                       "冗余的入门铺垫在这个阶段会占用工作记忆、反而降低理解（专家反转效应）"),
]


def domain_expertise(con, domain):
    """该 domain 的熟练度画像——决定讲解时该给多少引导。

    **为什么不能用 atom 自己的 level 来定引导量**：新 atom 永远是 level 0，
    照它来判断的话每个新知识点都要从最详细的四步模板讲起。但真正决定
    "该给多少引导"的是学习者在**这个领域整体**的熟练度：领域里已经建立
    了足够多的图式之后，入门铺垫就从"必要的支撑"变成"占用工作记忆的冗余"，
    实测中这会降低而非提高理解效率（Kalyuga 的 expertise reversal effect）。
    """
    if not domain:
        return None
    rows = con.execute(
        "SELECT level FROM atoms WHERE domain=? AND status!='retired'", (domain,)).fetchall()
    if not rows:
        return None
    applicable = sum(1 for r in rows if (r["level"] or 0) >= 3)
    proven = sum(1 for r in rows if (r["level"] or 0) >= 4)
    tier, guidance = next((t, g) for lim, t, g in GUIDANCE_TIERS if applicable < lim)
    return {"domain": domain, "atoms": len(rows), "applicable": applicable,
            "transfer_proven": proven, "tier": tier, "guidance": guidance}


def feedback_counts(con, kind, ref):
    r = con.execute("SELECT hits,misses FROM guidance_feedback WHERE kind=? AND ref=?",
                    (kind, str(ref))).fetchone()
    return (r["hits"], r["misses"]) if r else (0, 0)


def record_guidance_feedback(con, kind, refs, passed):
    """记一次"用了这些指导，随后的检验通过/失败"。

    挂在 `verify` 上而不是单独的命令：`verify` 是教学循环里每个 atom 都必跑的
    一步，模型顺手多传个 `--used-pref` 比记得再调一次 `pref hit` 可靠得多
    （同决策 18）。返回被自动降级的 pref 列表。
    """
    demoted = []
    col = "hits" if passed else "misses"
    tcol = "last_hit" if passed else "last_miss"
    for ref in refs or []:
        ref = str(ref)
        con.execute(
            f"INSERT INTO guidance_feedback(kind,ref,{col},{tcol}) VALUES(?,?,1,?) "
            f"ON CONFLICT(kind,ref) DO UPDATE SET {col}={col}+1, {tcol}=excluded.{tcol}",
            (kind, ref, now()))
        if passed:
            continue
        hits, misses = feedback_counts(con, kind, ref)
        # miss 够多、且明显多于 hit → 这条指导大概率没起作用。
        # pref 直接降回候选（可被新证据重新转正）；insight 只标记，
        # 因为它是 L1 画像、由 distill 走复核流程，不该被单条检验静默改写。
        if misses >= PREF_MISS_LIMIT and misses > hits and kind == "pref":
            row = con.execute("SELECT active,rule FROM teaching_prefs WHERE id=?",
                              (ref,)).fetchone()
            if row and row["active"]:
                con.execute("UPDATE teaching_prefs SET active=0 WHERE id=?", (ref,))
                demoted.append({"id": int(ref), "rule": row["rule"],
                                "hits": hits, "misses": misses})
    return demoted


def teaching_guidance(con, domain=None):
    """开讲前该知道的东西，一次性摆出来。

    **为什么挂在 `probe` 上而不是写成"记得先查"的指令**：`probe` 是教学
    循环里每个 atom 都必跑的第一步，是最稳定的挂载点（同 CLAUDE.md 决策 18）。
    研究上对应的是"选教学策略前先做状态推断"——多数生成式导师把认知诊断和
    教学决策纠缠在一次生成里，看到困惑就直接编一个解释，头几次往往文不对题。
    把诊断材料在开讲前摆好，是让这一步真的发生的唯一可靠办法。
    """
    g = {}
    exp = domain_expertise(con, domain)
    if exp:
        g["domain_expertise"] = exp
    # 有上限：probe 是教学循环里每个 atom 都跑的命令，它的输出会反复进上下文。
    # 画像本来就该少而精（同 evolution.md 的防膨胀），攒到超过上限说明该合并了。
    rows = con.execute(
        "SELECT key,value FROM profile WHERE key LIKE ? ORDER BY updated DESC LIMIT ?",
        (INSIGHT_PREFIX + "%", MAX_INSIGHTS_IN_GUIDANCE + 1)).fetchall()
    if rows:
        g["insights"] = {r["key"][len(INSIGHT_PREFIX):]: r["value"]
                         for r in rows[:MAX_INSIGHTS_IN_GUIDANCE]}
        if len(rows) > MAX_INSIGHTS_IN_GUIDANCE:
            g["insights_truncated"] = (
                f"画像超过 {MAX_INSIGHTS_IN_GUIDANCE} 条，这里只带了最近更新的几条。"
                "该合并同类画像了——每条都要占用每次 probe 的上下文预算")
    # 带上 id：模型要用 `verify --used-pref <id>` 回报这条规则管不管用
    rules = con.execute(
        "SELECT id, concept_type, rule FROM teaching_prefs "
        "WHERE active=1 AND retired=0 AND concept_type IS NOT NULL AND concept_type!='' "
        "ORDER BY evidence_count DESC LIMIT 12").fetchall()
    if rules:
        by_type = {}
        for r in rules:
            by_type.setdefault(r["concept_type"], []).append(
                {"id": r["id"], "rule": r["rule"]})
        g["rules_by_concept_type"] = by_type
    if g:
        g["hint"] = ("开讲前先判断这次要讲的东西属于哪种 concept_type，"
                     "命中 rules_by_concept_type / insights 就照它组织第一次讲解，"
                     "别等用户说没懂再补救。引导量按 domain_expertise.guidance 定。"
                     "**用了哪几条，检验时用 `verify --used-pref <id> --used-insight <type>` "
                     "回报**——否则一条错规则会一直把失败栽给「这个知识点难」。")
    return g or None


def atom_view(r):
    fs = _col(r, "fail_streak", 0)
    p = effective_recall(r["level"], r["last_verified"], _col(r, "stability", 1.0), fs)
    return {
        "slug": r["slug"], "name": r["name"], "domain": _col(r, "domain"),
        "level": r["level"], "level_name": LEVEL_NAME.get(r["level"], "?"),
        "recall": p, "last_verified": r["last_verified"],
        "verify_method": _col(r, "verify_method"), "status": _col(r, "status"),
        "fail_streak": fs, "difficulty": _col(r, "difficulty", 5.0),
        "stale": p < PROBE_THRESHOLD,
        # 从未被检验过 ≠ 不会。库里 level 0 只代表"没有数据"，用户完全可能
        # 早就会了——系统对"虚低"没有任何自动发现机制（探针只查已知的衰减，
        # 不会去测一个它以为你不懂的东西），所以只能把这个状态标出来，
        # 提醒开讲前先花十秒确认，别把已经会的东西从头讲一遍。
        "never_assessed": r["last_verified"] is None,
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
    target = con.execute("SELECT * FROM atoms WHERE slug=?", (a.slug,)).fetchone()
    if target is None:
        # 静默返回空 probe 列表是最坏的行为：看起来"没有前置需要探"，
        # 实际是 slug 打错了/atom 还没建，探针从此对它永久空转（同决策 15）。
        out({"error": f"atom 不存在：{a.slug}",
             "fix": "先 `add` 建好这个 atom，或用 `find` 确认正确的 slug"})
        return
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
    unknown = [x["slug"] for x in need if x.get("never_assessed")]
    if unknown or _col(target, "last_verified") is None:
        res["never_assessed_hint"] = (
            "标了 never_assessed 的（含目标本身）从没被检验过——那只代表没有数据，"
            "不代表用户不会。开讲前先花十秒问一句，答得上来就直接 "
            "`verify --method transfer` 跳过去，别从头讲一遍。")
    guidance = teaching_guidance(con, _col(target, "domain"))
    if guidance:
        res["teaching_guidance"] = guidance
    if dangling:
        res["dangling_prereqs"] = dangling
        res["warning"] = ("这些前置在库里不存在（多半是 slug 拼错）：" + ", ".join(dangling) +
                          "。请用 add 补建，或用 edge rm 删掉这条边——否则这些前置永远不会被探到")
    out(res)


def cmd_plan(a, con):
    """递归取出目标 atom 的全部前置闭包，按拓扑序（最深的祖先在前）返回。"""
    if not con.execute("SELECT 1 FROM atoms WHERE slug=?", (a.slug,)).fetchone():
        # 同 cmd_probe：静默返回空链看起来像"这个点没有前置要补"，
        # 实际可能只是 slug 拼错了
        out({"error": f"atom 不存在：{a.slug}",
             "fix": "先 `add` 建好这个 atom，或用 `find` 确认正确的 slug"})
        return
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
    res = {
        "topic": a.topic,
        "profile": {r["key"]: r["value"] for r in con.execute("SELECT * FROM profile")},
        "known_atoms": [atom_view(r) for r in atoms],
        "open_misconceptions": [dict(r) for r in misc],
        "teaching_rules": [dict(r) for r in prefs],
    }
    # 主题可能横跨多个 domain，各自的熟练度不同 → 引导量也该不同
    tiers = [e for e in (domain_expertise(con, d) for d in
                         dict.fromkeys(r["domain"] for r in atoms if r["domain"])) if e]
    if tiers:
        res["domain_expertise"] = tiers
    out(res)


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
    """记一次检验结果，并按结果调整掌握度、稳定度、难度。

    通过：level 提到该检验方式对应的上限；stability 用 FSRS 启发的公式放大——
          涨幅同时取决于难度（越易涨越多）和"这次通过有多意外"（检验发生时
          估算的回忆概率越低，通过后涨得越多，见 `update_stability()`）。
    失败：level 压到该方式之下；stability 用同一公式回落，难度越高、失败前
          回忆概率越低，惩罚相对越轻；连续 3 次失败标 blocked——硬刚同一个点
          是最伤学习动机的事，该止损就止损。
    """
    r = con.execute("SELECT * FROM atoms WHERE slug=?", (a.slug,)).fetchone()
    if not r:
        out({"error": "not found", "slug": a.slug}); return
    ml = METHOD_LEVEL[a.method]
    lvl, stab, streak, status = r["level"], float(r["stability"] or 1.0), r["fail_streak"], r["status"]
    diff = float(_col(r, "difficulty", 5.0) or 5.0)
    # 这次检验发生时、更新前的估算回忆概率。首次验证没有 last_verified，
    # recall_prob 会返回 0.0，但那是"没有数据"而非"预期已忘光"——见 FIRST_VERIFY_RECALL。
    r_now = recall_prob(r["last_verified"], stab) if r["last_verified"] else FIRST_VERIFY_RECALL
    passed = a.result == "pass"
    diff = update_difficulty(diff, passed, ml)
    stab = update_stability(stab, diff, passed, ml, r_now)
    if passed:
        lvl = max(lvl, ml)
        streak = 0
        status = "active" if status == "blocked" else status
    else:
        lvl = max(0, min(lvl, ml - 1))
        streak += 1
        if streak >= 3:
            status = "blocked"
    con.execute(
        "UPDATE atoms SET level=?, stability=?, difficulty=?, last_verified=?, verify_method=?, "
        "fail_streak=?, status=?, user_phrasing=COALESCE(?,user_phrasing), updated=? WHERE slug=?",
        (lvl, stab, diff, now(), a.method, streak, status, a.phrasing, now(), a.slug))
    con.commit()
    reindex(con, a.slug); con.commit()
    res = {"slug": a.slug, "result": a.result, "method": a.method,
           "level": lvl, "level_name": LEVEL_NAME[lvl], "stability_days": stab,
           "difficulty": diff, "fail_streak": streak, "status": status}

    # 这次讲解用了哪些教学指导 → 给它们记一次有效/无效。
    # 没传就什么都不记：宁可漏记，也不要把失败错误地栽给一条没被用到的规则。
    demoted = (record_guidance_feedback(con, "pref", a.used_pref, passed) +
               record_guidance_feedback(con, "insight", a.used_insight, passed))
    con.commit()
    if demoted:
        res["demoted_prefs"] = demoted
        res["advice_rules"] = ("这些教学规则连续失效已被降回候选——别再按它们教了。"
                               "如果你判断失败另有原因（内容确实难、前置没补），"
                               "用 `pref add` 追加一次证据把它转回来")
    if not (a.used_pref or a.used_insight):
        res["hint_feedback"] = ("这次没记 --used-pref/--used-insight。如果刚才的讲解"
                                "确实用到了 probe 给出的某条规则或画像，下次带上——"
                                "否则错误的教法会一直伪装成「这个知识点难」")
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
                     "stability", "difficulty", "source", "status", "notes"}:
            out({"error": f"字段不可写: {k}"}); return
        fields[k] = (int(v) if k == "level" else
                     (float(v) if k in ("stability", "difficulty") else v))
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
    """具体误区 + 跨领域的抽象错误类型（`error_type`）。

    `wrong_belief` 存用户原话（具体、不可迁移），`error_type` 存这条错误
    属于哪种**跨领域**的思维模式——"量词混淆""把对整体的陈述套到个体上"
    这类模式在任何学科都会重犯。只存具体误区的话，换个领域碰到同类错误
    完全不会被预防；有了类型标签，`--error-type` 能跨 domain 查出同类，
    `distill` 也能据此提炼成认知画像。设计上对称于 `teaching_prefs.concept_type`。
    """
    if a.action == "add":
        old = con.execute(
            "SELECT id,recurrence FROM misconceptions WHERE atom_slug=? AND wrong_belief=?",
            (a.atom, a.wrong)).fetchone()
        if old:
            con.execute("UPDATE misconceptions SET recurrence=recurrence+1, last_seen=?, "
                        "resolved=0, error_type=COALESCE(?,error_type) WHERE id=?",
                        (now(), a.error_type, old["id"]))
            mid = old["id"]
        else:
            cur = con.execute(
                "INSERT INTO misconceptions(atom_slug,wrong_belief,correction,error_type,"
                "first_seen,last_seen) VALUES(?,?,?,?,?,?)",
                (a.atom, a.wrong, a.correction, a.error_type, now(), now()))
            mid = cur.lastrowid
        con.commit()
        res = {"ok": True, "id": mid,
               "note": "复发次数高的误区，下次教到相关内容时主动提前拆解"}
        if not a.error_type:
            res["warning"] = ("没给 --error-type。具体误区只在这个 atom 上有用，"
                              "打上跨领域的错误类型（quantifier / type-mismatch / "
                              "causal-inversion / ignoring-probability 等）才能在别的领域预防同类错误")
        out(res)
    elif a.action == "retag":
        if not a.id or not a.error_type:
            out({"error": "retag 需要 --id 和 --error-type"}); return
        con.execute("UPDATE misconceptions SET error_type=? WHERE id=?", (a.error_type, a.id))
        con.commit(); out({"ok": True, "id": a.id, "error_type": a.error_type})
    elif a.action == "resolve":
        con.execute("UPDATE misconceptions SET resolved=1 WHERE id=?", (a.id,))
        con.commit(); out({"ok": True, "id": a.id})
    else:
        # --error-type 跨 domain 查同类错误模式（不限定 atom），这是它相对
        # --atom 的意义所在：换个领域教新东西前，先看看这类错误以前怎么犯的
        rows = con.execute(
            "SELECT * FROM misconceptions WHERE (?='' OR atom_slug LIKE ?) "
            "AND (?='' OR error_type=?) "
            "AND (resolved=0 OR ?=1) ORDER BY recurrence DESC, last_seen DESC LIMIT 50",
            (a.atom or "", f"%{a.atom or ''}%", a.error_type or "", a.error_type or "",
             1 if a.all else 0)).fetchall()
        out([dict(r) for r in rows])


def cmd_pref(a, con):
    """教学策略的三层进化。L1 直接生效；L2 需要 >=2 次同向证据才转正。

    门槛的意义：一次偶发反馈不该被固化成永久规则，否则规则集会互相打架、
    越滚越乱，最后没人敢信也没人敢删。

    `retire` 是"没人敢删"这个问题的解法：某条规则的前提不再成立时
    （比如判断依据的环境变了），标记它作废、写清理由，而不是留着和新
    规则互相矛盾，也不是真的删掉丢失审计轨迹。被 retire 的规则不会
    再被同文本证据复活——`add` 撞到它时会拒绝并提示原因。
    """
    if a.action == "add":
        row = con.execute("SELECT * FROM teaching_prefs WHERE rule=? AND scope=?",
                          (a.rule, a.scope)).fetchone()
        if row and row["retired"]:
            out({"error": f"id={row['id']} 这条规则已被作废（{row['retired_reason']}），"
                          "不能靠追加同文本证据复活。确有必要的话，写一条新规则（换个 scope 或措辞），"
                          "不要绕开 retire 的理由。"})
            return
        ev = {"date": now(), "signal": a.evidence or ""}
        if row:
            evs = json.loads(row["evidence"] or "[]"); evs.append(ev)
            cnt = row["evidence_count"] + 1
            active = 1 if (row["layer"] == "L1" or cnt >= 2) else 0
            con.execute("UPDATE teaching_prefs SET evidence=?, evidence_count=?, active=?, "
                        "concept_type=COALESCE(?,concept_type) WHERE id=?",
                        (json.dumps(evs, ensure_ascii=False), cnt, active, a.concept_type, row["id"]))
            pid, promoted = row["id"], bool(active and not row["active"])
        else:
            active = 1 if a.layer == "L1" else 0
            cur = con.execute(
                "INSERT INTO teaching_prefs(layer,scope,concept_type,rule,polarity,evidence,"
                "evidence_count,active,created) VALUES(?,?,?,?,?,?,1,?,?)",
                (a.layer, a.scope, a.concept_type, a.rule, a.polarity,
                 json.dumps([ev], ensure_ascii=False), active, now()))
            pid, promoted = cur.lastrowid, bool(active)
        con.commit()
        out({"ok": True, "id": pid, "active": bool(active), "promoted": promoted,
             "note": "L2 规则需 2 次同向证据才生效；候选规则会一直挂着直到被证实或过期"})
    elif a.action == "retag":
        # 补打/改打 concept_type，不算新证据——用于给已有规则补上跨领域维度的标签。
        if not a.id or not a.concept_type:
            out({"error": "retag 需要 --id 和 --concept-type"}); return
        con.execute("UPDATE teaching_prefs SET concept_type=? WHERE id=?",
                    (a.concept_type, a.id))
        con.commit()
        out({"ok": True, "id": a.id, "concept_type": a.concept_type})
    elif a.action == "hit":
        # 手动记一次命中。常规路径是 `verify --used-pref`（挂在必跑的命令上，
        # 更可靠）；这条留给"用了规则但当下没有对应检验"的场合。
        record_guidance_feedback(con, "pref", [a.id], True); con.commit()
        h, m = feedback_counts(con, "pref", a.id)
        out({"ok": True, "id": a.id, "hits": h, "misses": m})
    elif a.action == "miss":
        demoted = record_guidance_feedback(con, "pref", [a.id], False); con.commit()
        h, m = feedback_counts(con, "pref", a.id)
        out({"ok": True, "id": a.id, "hits": h, "misses": m,
             "demoted": bool(demoted),
             "note": f"累计 {m} 次失效、{h} 次有效；miss≥{PREF_MISS_LIMIT} 且多于 hit 会自动降回候选"})
    elif a.action == "retire":
        # 规则被证伪、或前提条件（比如渲染环境）不再成立时用这个，而不是留着
        # 互相矛盾的规则等 90 天 prune——prune 只处理 active=1 的规则，且只是
        # 降级为候选，不会阻止它靠新证据复活。retire 是明确、带理由、不可被
        # 同文本证据复活的终态，取代记录仍在（可追溯），不是删除。
        if not a.id or not a.reason:
            out({"error": "retire 需要 --id 和 --reason"}); return
        row = con.execute("SELECT id FROM teaching_prefs WHERE id=?", (a.id,)).fetchone()
        if not row:
            out({"error": f"没有 id={a.id} 这条规则"}); return
        con.execute("UPDATE teaching_prefs SET retired=1, retired_reason=?, retired_at=?, "
                    "active=0 WHERE id=?", (a.reason, now(), a.id))
        con.commit()
        out({"ok": True, "id": a.id, "retired_reason": a.reason})
    elif a.action == "prune":
        # 90 天没被命中的生效规则降为候选，防止规则集无限膨胀。
        #
        # `last_hit` 依赖模型自觉调用 `pref hit`——这是个纯文字指令，换任何
        # 模型跑、或者模型记忆被清空重新部署，都不能保证被记得执行（这不是
        # 哪个模型更笨的问题，是纯文字指令天然的不可靠性）。所以这里不能
        # 只信 last_hit：额外算一个"这个 scope 对应的领域最近是否还在被教"
        # 的代理信号（scope 匹配的 atom 最近有没有被 verify 过）作为兜底——
        # 只要该领域最近还在活跃教学，就不判定规则过期，哪怕 hit 从没被调用。
        # 宁可少剪掉几条其实没用的规则，也不要错误剪掉一条其实还在起作用、
        # 只是没被显式记录命中的规则。
        rows = con.execute(
            "SELECT p.id, p.scope, p.created, f.last_hit FROM teaching_prefs p "
            "LEFT JOIN guidance_feedback f ON f.kind='pref' AND f.ref=CAST(p.id AS TEXT) "
            "WHERE p.active=1 AND p.layer='L2'").fetchall()
        stale = []
        for r in rows:
            base_days = days_since(r["last_hit"] or r["created"]) or 0
            if base_days <= 90:
                continue
            if r["scope"] == "global":
                recent = con.execute(
                    "SELECT MAX(updated) m FROM atoms").fetchone()["m"]
            elif r["scope"].startswith("domain:"):
                dom = r["scope"].split(":", 1)[1]
                recent = con.execute(
                    "SELECT MAX(updated) m FROM atoms WHERE domain LIKE ?",
                    (f"%{dom}%",)).fetchone()["m"]
            elif r["scope"].startswith("atom:"):
                slug = r["scope"].split(":", 1)[1]
                recent = con.execute(
                    "SELECT updated m FROM atoms WHERE slug=?", (slug,)).fetchone()
                recent = recent["m"] if recent else None
            else:
                recent = None
            if recent and (days_since(recent) or 999) <= 90:
                continue  # 领域仍活跃，即使没人调用过 pref hit 也保留
            stale.append(r["id"])
        for i in stale:
            con.execute("UPDATE teaching_prefs SET active=0 WHERE id=?", (i,))
        con.commit(); out({"demoted": stale, "count": len(stale),
                            "note": "既没被pref hit命中过，对应领域也超过90天没有新的atom活动，才会被降级"})
    else:
        # hit/miss 只从 guidance_feedback 读——teaching_prefs.hit_count/last_hit
        # 是旧列，已停止写入（migrate 把历史值搬过去了），留着只为不破坏老库。
        rows = con.execute(
            "SELECT p.id,p.layer,p.scope,p.concept_type,p.rule,p.polarity,p.evidence_count,"
            "p.active,p.retired,p.retired_reason,"
            "COALESCE(f.hits,0) hits, COALESCE(f.misses,0) misses, f.last_hit, f.last_miss "
            "FROM teaching_prefs p "
            "LEFT JOIN guidance_feedback f ON f.kind='pref' AND f.ref=CAST(p.id AS TEXT) "
            "WHERE (?=1 OR p.active=1) AND (?='' OR p.concept_type=?) "
            "ORDER BY p.active DESC, p.evidence_count DESC",
            (1 if a.all else 0, a.concept_type or "", a.concept_type or "")).fetchall()
        out([dict(r) for r in rows])


def cmd_distill(a, con):
    """检测哪些 concept_type / error_type 已经积累够了、该提炼成认知画像。

    **为什么需要这个命令**：`teaching_prefs` 和 `misconceptions` 存的都是具体
    记录，只有在你主动查询到对应的 scope/atom 时才会被用上。碰到一个全新的、
    没有任何现成记录匹配的情形时，这些积累完全帮不上忙——只能凭本能重新试错。
    把同类记录提炼成一句更概括的认知特征写进 `profile`（L1，每次 `context`
    /`status` 都会自动带出来，不需要主动查），新情形才有方向可循。

    **为什么是命令而不是写在 SKILL.md 里的提醒**：同决策 18——"记得定期提炼"
    这种纯文字指令不可能被可靠执行。这个命令把"该提炼了"变成一个**能被算出来**
    的状态：记录数够阈值、且 profile 里没有对应画像（或画像比最新记录旧），
    就报出来。它自己不做提炼（那是判断性工作，只有模型能做），但它保证
    "需要提炼"这件事不会因为没人想起来而永远不发生。

    输出里带每个待提炼类型下的**全部具体记录**，读完直接写 profile：
        profile --set "insight:notation=<提炼出的一句话>"
    """
    prof = {r["key"]: r for r in con.execute("SELECT key,value,updated FROM profile")}
    items = []

    groups = con.execute(
        "SELECT concept_type t, COUNT(*) n FROM teaching_prefs "
        "WHERE active=1 AND retired=0 AND concept_type IS NOT NULL AND concept_type!='' "
        "GROUP BY concept_type").fetchall()
    for g in groups:
        rows = con.execute(
            "SELECT id,scope,rule,polarity,evidence,created FROM teaching_prefs "
            "WHERE active=1 AND retired=0 AND concept_type=? ORDER BY evidence_count DESC",
            (g["t"],)).fetchall()
        items.append(("concept_type", g["t"], g["n"], DISTILL_MIN_PREFS,
                      max((r["created"] or "" for r in rows), default=""),
                      [{"id": r["id"], "scope": r["scope"], "rule": r["rule"],
                        "polarity": r["polarity"]} for r in rows]))

    groups = con.execute(
        "SELECT error_type t, COUNT(*) n FROM misconceptions "
        "WHERE error_type IS NOT NULL AND error_type!='' GROUP BY error_type").fetchall()
    for g in groups:
        # 已 resolve 的误区照样计入：它仍然是"这个学习者在这类问题上栽过"的证据，
        # 对提炼认知画像有价值。但要标出来，否则模型看不出这条是历史还是现存问题。
        rows = con.execute(
            "SELECT id,atom_slug,wrong_belief,correction,recurrence,resolved,last_seen "
            "FROM misconceptions WHERE error_type=? ORDER BY recurrence DESC",
            (g["t"],)).fetchall()
        items.append(("error_type", g["t"], g["n"], DISTILL_MIN_MISC,
                      max((r["last_seen"] or "" for r in rows), default=""),
                      [{"id": r["id"], "atom": r["atom_slug"], "wrong": r["wrong_belief"],
                        "correction": r["correction"], "recurrence": r["recurrence"],
                        "resolved": bool(r["resolved"])}
                       for r in rows]))

    pending, ok = [], []
    for kind, t, n, need, newest, records in items:
        key = INSIGHT_PREFIX + t
        existing = prof.get(key)
        if n < need and not existing:
            continue  # 还没积累够，不用管
        # 已有画像但之后又攒了新记录 → 画像过时了，值得重写
        stale = bool(existing and newest and (existing["updated"] or "") < newest)
        # 画像也要能被证伪：`verify --used-insight` 累计的 miss 说明照它教反而更糟。
        # 不像 pref 那样自动降级——画像是 L1，该由人/模型复核后重写，不该被
        # 单条检验静默改掉。另外太久没复核的也报出来（记录会漂，画像不会自己更新）。
        hits, misses = feedback_counts(con, "insight", t)
        ineffective = misses >= PREF_MISS_LIMIT and misses > hits
        overdue = bool(existing and (days_since(existing["updated"]) or 0) > INSIGHT_REVIEW_DAYS)
        if existing and not stale and not ineffective and not overdue:
            ok.append({"kind": kind, "type": t, "records": n,
                       "insight": existing["value"], "hits": hits, "misses": misses})
            continue
        if existing and (ineffective or overdue) and not stale:
            pending.append({
                "kind": kind, "type": t, "records": n, "threshold": need,
                "reason": (f"照这条画像教失败了 {misses} 次、只成功 {hits} 次，多半是画像本身有问题"
                           if ineffective else
                           f"超过 {INSIGHT_REVIEW_DAYS} 天没复核过，底下的记录可能已经漂了"),
                "current_insight": existing["value"],
                "hits": hits, "misses": misses,
                "write_to": key, "source_records": records,
            })
            continue
        pending.append({
            "kind": kind, "type": t, "records": n, "threshold": need,
            "reason": "已有画像但之后又新增了记录，值得重写" if stale else "首次达到提炼阈值",
            "current_insight": existing["value"] if existing else None,
            "write_to": key,
            "source_records": records,
        })

    out({
        "pending": pending,
        "up_to_date": ok,
        "hint": ("对 pending 里每一项：读完 source_records，提炼成一句可执行的认知特征"
                 "（说清楚'该怎么教'而不是'用户是什么样的人'），然后 "
                 f"`profile --set \"{INSIGHT_PREFIX}<type>=<提炼结果>\"`。"
                 "pending 为空说明暂时没有够格提炼的类型，不要硬凑。"),
    })


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


def collect_structural_issues(con):
    """代码能**确定判定对错**的完整性问题。`doctor` 和 `review` 共用这一份实现——
    两处各写一遍就是复审清单里那个"同一个概念两处实现"的坑。

    这里只放确定性判断。"这两条误区看起来可能不是一类"那种需要语义理解的疑点
    不属于这里，它们走 `review` 的 needs_judgment——混进来会毁掉 doctor 的
    强承诺：`issue_count: 0` 现在的含义是"确定没问题"，一旦掺入待确认的疑点，
    这个数字就没法解读了。
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
    # 标签漂移：同一个概念被写成几个字面不同的标签（大小写、单复数、连字符），
    # 会把本该聚在一起的记录拆散——distill 永远达不到阈值，跨领域查询也漏。
    # 这是纯静默的失败：标签看着都在，抽象层实际已经失效。
    def _norm(s):
        return (s or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "").rstrip("s")
    for table, col, fix_cmd in (
            ("misconceptions", "error_type", "misconception retag --id <id> --error-type <统一后的值>"),
            ("teaching_prefs", "concept_type", "pref retag --id <id> --concept-type <统一后的值>"),
            ("atoms", "domain", "set <slug> --field domain=<统一后的值>")):
        vals = [r[0] for r in con.execute(
            f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL AND {col}!=''")]
        groups = {}
        for v in vals:
            groups.setdefault(_norm(v), []).append(v)
        for k, variants in groups.items():
            if len(variants) > 1:
                issues.append({"kind": "tag_drift", "detail": f"{table}.{col}: " + " / ".join(
                    repr(v) for v in variants),
                    "fix": f"这几个标签归一化后是同一个，会让同类记录聚不到一起。用 {fix_cmd} 统一"})

    n_atoms = con.execute("SELECT COUNT(*) c FROM atoms").fetchone()["c"]
    n_fts = con.execute("SELECT COUNT(*) c FROM atoms_fts").fetchone()["c"]
    if n_atoms != n_fts:
        issues.append({"kind": "fts_out_of_sync",
                       "detail": f"atoms={n_atoms} 但 atoms_fts={n_fts}",
                       "fix": "learndb.py doctor --repair-fts（中文模糊查找会漏结果）"})
    return issues


def cmd_doctor(a, con):
    """数据完整性体检。表结构里没有外键约束（加 FK 要重建已有表，代价大于收益），
    所以引用完整性靠写入时校验 + 这个命令定期兜底。

    大改动之后、或者觉得探针/进度显示不对劲时跑一下。
    需要**语义层面**的体检（分类打错、画像过时、规则互相矛盾）用 `review`。
    """
    issues = collect_structural_issues(con)
    if a.repair_fts:
        reindex(con); con.commit()
        issues = [i for i in issues if i["kind"] != "fts_out_of_sync"]
        out({"repaired": "atoms_fts 已重建", "remaining_issues": issues, "count": len(issues)})
        return
    out({"ok": not issues, "issue_count": len(issues), "issues": issues,
         "schema_version": SCHEMA_VERSION,
         "hint": "全部为空说明数据完整性没问题" if not issues else
                 "按每条的 fix 处理；这些问题不会自己消失，而且大多是静默失败"})


def cmd_review(a, con):
    """完整体检：用户主动要求"检查一下知识库"时跑这个。

    **和 `doctor` 的分工**：`doctor` 只做代码能确定判定的结构检查；`review`
    在此之上加两类 `doctor` 结构上做不了的东西——需要**语义判断**的疑点
    （分类打错、画像过时、规则互相矛盾），以及**值得看一眼但不算错**的状态。
    三类严格分开，因为它们的可信度不同：structural 是确定的问题，
    needs_judgment 是待人工/模型确认的材料，attention 只是提醒。

    **为什么挂在用户主动请求上**：这类检查需要判断力、消耗上下文，不适合高频
    自动跑；而这个系统一路踩的最大的坑就是依赖模型记得做某件事（决策 18）。
    用户开口是 100% 可靠的触发器，比任何写在 SKILL.md 里的提醒都强。

    代码只负责**把材料摆出来**，判断留给模型——同 `distill` 的思路。
    """
    res = {}

    issues = collect_structural_issues(con)
    res["structural"] = {"count": len(issues), "issues": issues,
                         "note": "代码确定判定的问题，按每条的 fix 直接处理"}

    # ---- 需要语义判断的材料 ----
    judgment = {}

    # ① 分类同质性：doctor 只能查拼写漂移（notation vs Notation），查不出
    # "把 causal-inversion 错打成 quantifier" 这种语义错误。后果是双向的：
    # 打进去的那类被污染（distill 从混杂记录里提炼出模糊画像，而画像是全局
    # 生效的），本该属于的那类饿死（永远达不到提炼阈值）。
    cohesion = []
    for t, in con.execute("SELECT DISTINCT error_type FROM misconceptions "
                          "WHERE error_type IS NOT NULL AND error_type!=''"):
        rows = con.execute(
            "SELECT id,atom_slug,wrong_belief,correction FROM misconceptions "
            "WHERE error_type=? ORDER BY recurrence DESC LIMIT ?", (t, a.limit)).fetchall()
        if len(rows) > 1:
            cohesion.append({"kind": "error_type", "type": t,
                             "records": [{"id": r["id"], "atom": r["atom_slug"],
                                          "wrong": r["wrong_belief"],
                                          "correction": r["correction"]} for r in rows]})
    for t, in con.execute("SELECT DISTINCT concept_type FROM teaching_prefs "
                          "WHERE concept_type IS NOT NULL AND concept_type!='' AND retired=0"):
        rows = con.execute(
            "SELECT id,scope,rule FROM teaching_prefs WHERE concept_type=? AND retired=0 "
            "ORDER BY evidence_count DESC LIMIT ?", (t, a.limit)).fetchall()
        if len(rows) > 1:
            cohesion.append({"kind": "concept_type", "type": t,
                             "records": [dict(r) for r in rows]})
    if cohesion:
        judgment["tag_cohesion"] = {
            "groups": cohesion,
            "ask": "逐组看：这些记录真的是同一类吗？判断标准是**补救策略是不是同一套**，"
                   "不是'看起来像不像'。混进来的用 `misconception retag` / `pref retag` 挪走。"}

    # ② 画像是否还准确：画像写进 profile 后每次 probe 都生效，但从没有任何
    # 机制回头验证它当初的提炼是否仍然成立（distill 只在有新记录时才提示重写）。
    insights = []
    for r in con.execute("SELECT key,value,updated FROM profile WHERE key LIKE ?",
                         (INSIGHT_PREFIX + "%",)):
        t = r["key"][len(INSIGHT_PREFIX):]
        hits, misses = feedback_counts(con, "insight", t)
        src = [x[0] for x in con.execute(
            "SELECT wrong_belief FROM misconceptions WHERE error_type=? LIMIT ?",
            (t, a.limit))] or [x[0] for x in con.execute(
            "SELECT rule FROM teaching_prefs WHERE concept_type=? AND retired=0 LIMIT ?",
            (t, a.limit))]
        insights.append({"type": t, "insight": r["value"], "written": (r["updated"] or "")[:10],
                         "hits": hits, "misses": misses, "based_on": src})
    if insights:
        judgment["insight_accuracy"] = {
            "items": insights,
            "ask": "对每条：底下的 based_on 记录还支持这句画像吗？misses 明显多于 hits 的"
                   "说明照它教反而更糟。要改就 `profile --set \"insight:<type>=...\"` 重写。"}

    # ③ 规则互相矛盾：evolution.md 开篇就把"前后矛盾"列为自进化的头号风险
    # （"多举例子"和"别啰嗦"同时生效），但至今没有任何检测——规则攒到几十条
    # 之后这几乎必然发生，且完全无声。
    rules = con.execute(
        "SELECT id,scope,concept_type,rule,polarity FROM teaching_prefs "
        "WHERE active=1 AND retired=0 ORDER BY scope,concept_type").fetchall()
    if len(rules) > 1:
        judgment["rule_conflicts"] = {
            "active_rules": [dict(r) for r in rules],
            "ask": "有没有两条在同一场合会给出相反指令的？（经典例子：'多举例子'和"
                   "'别啰嗦'同时生效）。有的话用 `pref retire --id --reason` 废掉过时的那条，"
                   "不要留着互相打架。"}

    # ---- 值得看一眼、但不算"错" ----
    attention = {}

    # 叶子节点：探针只查"某个正在学的内容的直接前置"，没有下游的 atom 结构上
    # 永远碰不到。这里只在**用户主动要求体检时**列出，且只列不出题——这跟
    # 决策 4 否决的"主动复习提醒"是两回事，那条否决的是主动出题打断学习。
    leaves = []
    for r in con.execute("SELECT * FROM atoms WHERE status='active'"):
        if con.execute("SELECT 1 FROM edges WHERE from_slug=? AND type='prereq'",
                       (r["slug"],)).fetchone():
            continue
        v = atom_view(r)
        if v["recall"] < DUE_THRESHOLD and r["level"] >= 2:
            leaves.append({"slug": r["slug"], "name": r["name"], "recall": v["recall"],
                           "level": v["level_name"]})
    if leaves:
        attention["stale_leaves"] = {
            "items": sorted(leaves, key=lambda x: x["recall"])[:a.limit],
            "note": "这些没有下游 atom，探针结构上够不到，可能已经忘了却无人察觉——"
                    "而学习线末端往往正是最核心的结论。**只是列出，不要主动出题**，"
                    "用户想复习会自己开口。"}

    blocked = [dict(r) for r in con.execute(
        "SELECT slug,name,fail_streak FROM atoms WHERE status='blocked'")]
    if blocked:
        attention["blocked_atoms"] = {"items": blocked,
                                      "note": "连续失败被止损的，换个讲法/补前置后可以重开"}

    n_cand = con.execute("SELECT COUNT(*) c FROM teaching_prefs "
                         "WHERE active=0 AND retired=0").fetchone()["c"]
    if n_cand >= 5:
        attention["candidate_pileup"] = {
            "count": n_cand,
            "note": "候选规则堆积。要么证据门槛偏高，要么这些本就该 retire 掉"}

    isolated = [r["slug"] for r in con.execute("SELECT slug FROM atoms")
                if not con.execute("SELECT 1 FROM edges WHERE from_slug=? OR to_slug=?",
                                   (r["slug"], r["slug"])).fetchone()]
    if isolated:
        attention["isolated_atoms"] = {
            "items": isolated[:a.limit],
            "note": "既没有前置也没有下游，可能是路线图断了或 slug 拼错"}

    thin = [d for d, c in con.execute(
        "SELECT domain,COUNT(*) FROM atoms WHERE domain IS NOT NULL "
        "GROUP BY domain HAVING COUNT(*)=1")]
    if thin:
        attention["thin_domains"] = {
            "items": thin, "note": "只有 1 个 atom 的领域，可能是 domain 名打错了"}

    res["needs_judgment"] = judgment
    res["attention"] = attention
    res["hint"] = (
        "三块可信度不同，别混着报给用户：structural 是**确定的问题**（照 fix 修）；"
        "needs_judgment 是**待你判断的材料**，逐条读完再下结论，别假设现状就是对的；"
        "attention 只是**值得看一眼**，多数时候无需处理。"
        "读完给用户一份人话报告 + 修复清单，**等用户确认再动手改**。")
    out(res)


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
        exp = domain_expertise(con, None if d == "(未分类)" else d)
        tier = f"（当前引导档位：**{exp['tier']}**，{exp['applicable']}/{exp['atoms']} 个已达能应用）" if exp else ""
        lines.append(f"\n## {d}\n{tier}\n")
        lines.append("| 知识点 | 掌握度 | 估算回忆概率 | 难度 | 上次验证 | 状态 |")
        lines.append("|---|---|---|---|---|---|")
        for r in rows:
            v = atom_view(r)
            rec = v["recall"] if r["level"] >= 2 else "—"
            lines.append(f"| {r['name']} (`{r['slug']}`) | {v['level_name']} | "
                         f"{rec} | {_col(r, 'difficulty', '—')} | "
                         f"{(r['last_verified'] or '—')[:10]} | {r['status']} |")
    misc = con.execute(
        "SELECT * FROM misconceptions WHERE resolved=0 ORDER BY recurrence DESC").fetchall()
    if misc:
        lines.append("\n## 未解决的理解误区\n")
        for m in misc:
            et = f"［{m['error_type']}］" if _col(m, "error_type") else ""
            lines.append(f"- **{m['atom_slug'] or '通用'}**{et} — 误：{m['wrong_belief']}　→　"
                         f"正：{m['correction']}（出现 {m['recurrence']} 次）")
    prefs = con.execute("SELECT * FROM teaching_prefs WHERE active=1").fetchall()
    if prefs:
        lines.append("\n## 生效中的教学策略\n")
        for p in prefs:
            mark = "✅" if p["polarity"] == "works" else "🚫"
            ct = f"［{p['concept_type']}］" if _col(p, "concept_type") else ""
            lines.append(f"- {mark} [{p['scope']}]{ct} {p['rule']}（证据 {p['evidence_count']} 次）")

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
    p.add_argument("--used-pref", dest="used_pref", nargs="*", default=[],
                   help="这次讲解用到的 pref id（probe 的 teaching_guidance 里有）。"
                        "通过→记一次有效，失败→记一次失效；连续失效会自动降回候选。"
                        "不传就不记——错误的教法会一直伪装成「这个知识点难」")
    p.add_argument("--used-insight", dest="used_insight", nargs="*", default=[],
                   help="这次讲解用到的 insight 类型（如 notation）。同上，但画像不会被"
                        "单条检验静默改写，只由 distill 报出来要求复核")
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
    p.add_argument("action", choices=["add", "list", "resolve", "retag"])
    p.add_argument("--atom"); p.add_argument("--wrong"); p.add_argument("--correction")
    p.add_argument("--id", type=int); p.add_argument("--all", action="store_true")
    p.add_argument("--error-type", dest="error_type",
                   help="跨领域的错误模式，独立于 atom：这条错误属于哪种思维偏差，"
                        "而不是出在哪个知识点。常见值：quantifier(存在性/全称混淆)、"
                        "type-mismatch(把对整体的陈述套到个体上)、causal-inversion"
                        "(因果或顺序倒置)、ignoring-probability(只看典型路径不看期望)。"
                        "list 时可用同一参数跨 domain 查同类错误")
    p.set_defaults(fn=cmd_misconception)

    p = sub.add_parser("distill", help="检测哪些 concept_type/error_type 该提炼成认知画像了")
    p.set_defaults(fn=cmd_distill)

    p = sub.add_parser("review", help="完整体检（用户主动要求'检查知识库'时用）："
                                      "结构问题 + 需要语义判断的疑点 + 值得看一眼的状态")
    p.add_argument("--limit", type=int, default=10, help="每组材料最多列几条，控制上下文开销")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("pref", help="教学策略进化（L1 自动 / L2 需两次证据）")
    p.add_argument("action", choices=["add", "list", "hit", "miss", "prune", "retire", "retag"])
    p.add_argument("--rule"); p.add_argument("--scope", default="global")
    p.add_argument("--layer", default="L2", choices=["L1", "L2"])
    p.add_argument("--polarity", default="works", choices=["works", "fails"])
    p.add_argument("--evidence"); p.add_argument("--id", type=int)
    p.add_argument("--reason", help="retire 专用：为什么这条规则被作废/取代")
    p.add_argument("--concept-type", dest="concept_type",
                    help="跨领域维度，独立于 scope：这条规则是关于哪种教学情形，"
                         "而不是哪个领域。常见值：notation(引入新符号/记号)、"
                         "comparison(对比/类比题)、process(讲解流程/步骤)、"
                         "quantifier(存在性/全称这类措辞精确度)、test-design(检验题设计)。"
                         "list 时可用同一参数过滤，跨 domain 复用同类规则")
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
