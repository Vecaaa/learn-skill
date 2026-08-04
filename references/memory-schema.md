# 记忆层参考

需要不常用的库操作、或想理解掌握度/遗忘模型时读这份。

## 目录

- [为什么是 SQLite](#为什么是-sqlite)
- [文件布局](#文件布局)
- [表结构](#表结构)
- [大目标模式的三张表](#大目标模式的三张表)
- [掌握度与遗忘模型](#掌握度与遗忘模型)
- [命令全表](#命令全表)
- [上下文预算](#上下文预算)
- [常见操作范式](#常见操作范式)
- [Schema 迁移](#schema-迁移)

---

## 为什么是 SQLite

不是为了规模，是因为这个 skill 的核心操作恰好全是关系型查询：

| 操作 | 纯文件的问题 | SQLite |
|---|---|---|
| 前置依赖闭包（教 X 前找出所有未掌握的祖先） | 要把整库读进上下文再让模型推 | 递归 CTE 一条查询 |
| 用户模糊说法定位 atom（"那个栈的东西"） | 模型全文扫，库一大就漏 | FTS5 trigram 索引 |
| 按掌握度 + 时间算回忆概率排序 | 模型手算，不可靠 | SQL 算，确定性 |
| 只取相关子图进上下文 | 难以精确切片 | WHERE 精确切片 |
| 格式漂移 | 几周后必然发生 | schema 强制 |

**trigram 分词器**是刻意选的：默认的 unicode61 不切中文词，
中文查询会几乎全漏。trigram 支持子串匹配，中英文都能用（需 SQLite ≥ 3.34）。

真源是 `.db`；`knowledge.md` 是导出的只读视图，给用户翻阅和 git 备份用。

---

## 文件布局

```
~/.claude/learning/
├── learn.db          # 唯一真源
├── knowledge.md      # export 生成的可读快照
├── sources.jsonl     # 站点注册表（见 research.md），不含凭据
└── backups/          # backup 生成，保留最近 20 份
```

可用环境变量 `LEARN_DB` 覆盖数据库路径（多套知识库、或测试时有用）。

---

## 表结构

### `atoms` — 核心

| 字段 | 说明 |
|---|---|
| `slug` | 主键，kebab-case，**稳定不改**（改了依赖边会断） |
| `name` | 显示名 |
| `domain` | 领域，`context` 命令按它切片 |
| `aliases` | 换行分隔。中英文别名、俗称、**用户的错叫法**都往里塞——`find` 靠它 |
| `user_phrasing` | 用户复述时的原话。后续教学复用，理解成本明显下降 |
| `level` | 0 未学 / 1 见过 / 2 能复述 / 3 能应用 / 4 能迁移 |
| `stability` | 遗忘曲线的 S（天），每次验证通过变大 |
| `last_verified` / `verify_method` | 上次检验的时间和方式 |
| `source` | 出处，用于日后回溯校正 |
| `status` | `active` / `blocked`（连续 3 次失败）/ `retired` |
| `fail_streak` | 连续失败计数，`verify` 自动维护 |

### `edges` — 依赖图

`from_slug` 是 `to_slug` 的前置。`type` 为 `prereq`（真前置）或 `related`（相关）。

**只把真前置连成 `prereq`**：不懂 A 就真的学不了 B。"知道了更好"用 `related`。
前置图虚胖会导致每节课前触发一堆无意义探针。

### `misconceptions` — 价值最高的一张表

比"用户会什么"更有用。存用户**原话**的错误说法 + 正确版本 + 复发次数。
复发次数高的，下次教到相关内容时主动提前拆解；也是出 `debug` 题的最好素材。

### `teaching_prefs` — 自进化产物

见 `evolution.md`。`layer` 区分 L1（用户模型，自动生效）和 L2（教学策略，需两次证据）。
`polarity` 区分 `works` / `fails`——**失败的教法同样要记**。

### `sessions` — 会话记账

每次结束写 ≤5 行摘要 + 观察到的信号。不要写长篇，长篇没人看也没法聚合。

---

## 大目标模式的三张表

只有用户提出跨月的大目标（"学一年做出 X"）时才会用到，日常单点学习完全不涉及。
三张表构成 `goal → phase → project` 三层，且都直接落库——**路线图必须真的写进
这几张表，不能只在对话里描述一遍**，否则下次会话问"计划到哪了"就答不出来。

### `goals` — 大目标本身

| 字段 | 说明 |
|---|---|
| `north_star` | 目标澄清后"成功具体长什么样"，防止"字面意义"的误解（例：字面 GPT-3 不现实，
真正目标是理解并复现 GPT-2 规模架构） |
| `weekly_hours` | 用户每周可投入时间，仅用于估算换算，不是承诺 |
| `status` | `active` / `paused` / `done` |

### `phases` — 里程碑

**用验收标准定义边界，不用日历。**

| 字段 | 说明 |
|---|---|
| `objective` | "到这一步你应该能做什么"，不是日期 |
| `detail_level` | `coarse`（只有里程碑名+验收标准）/ `detailed`（已展开到 atom+project）。
只有当前阶段和下一阶段该是 `detailed`——这是"近详远粗"的落地字段 |
| `est_weeks` / `actual_weeks` | 软性估计 vs 实际耗时，`phase close` 时用后者滚动校准前者 |
| `retrospective` | 阶段结束时的复盘笔记 |

`atoms` 表有一个可选的 `phase_id` 字段，把知识点挂到某个阶段上
（`add --phase <id>`），`phase get`/`phase_progress()` 靠它统计这个阶段的
掌握进度。非 goal 模式下这个字段留空，不影响任何东西。

### `projects` — 阶段内的巩固项目

**`scaffold_provided` 和 `user_scope` 是硬性要求，不是可选字段**
（命令行没有强制校验内容质量，但缺失会警告）：

| 字段 | 说明 |
|---|---|
| `scaffold_provided` | 已经帮用户写好的部分，及为什么（工程量大但不是本阶段要学的东西） |
| `user_scope` | 用户自己要设计/写的那一小块，对应哪些 atom |
| `est_effort` | 粗略工作量描述，如"1-2 次学习会话"——项目体量必须小，
超了就砍范围（做核心切片，完整版标"可选"），不延长时间 |

`project_atoms` 是项目和 atom 的多对多关联表。**项目完成（`project done`）
不会自动给关联 atom 升级掌握度**——它只是列出关联 atom 提醒你，
真正的升级仍然走正常的 `verify --method transfer`。这样设计是为了让"项目"
成为 atom 走到"能迁移"这一档的自然载体，而不是绕开检验体系的另一套机制。

---

## 掌握度与遗忘模型

### 回忆概率

```
R = exp(-t / S)      t = 距上次验证的天数，S = stability
```

这不是精确的记忆科学，只是一个够用的排序信号。它的作用是决定
**"要不要在开讲前探一下"**，不是替代真实检验。

**两条强制修正**（都在 `effective_recall()` 里，任何地方算 recall 都必须走这个
函数，不要各写各的）：

- `level < 2` → 归零。回忆概率对没学扎实的 atom 没有意义
- `fail_streak > 0` → 归零。**这条是修一个真 bug**：`verify --result fail`
  会把 `last_verified` 刷成当下，于是 `R = exp(-0/S) = 1.0`。一个 level 3 的
  atom 挂掉 `transfer` 检验后 level 不变（挂掉 level-4 的题不否定 level-3 能力），
  recall 却是满值 → `probe` 认为它很牢固**直接跳过**。用户刚答错的东西，
  同一会话里再教依赖它的内容时探针视而不见——正好漏掉最该探的情况

- `R < 0.7` → `probe` 认为值得探针
- `R < 0.6` → `due` 认为已经开始忘（仅用户主动要求复习时才查）

### 验证后的更新

| 结果 | level | stability |
|---|---|---|
| 通过 | 提到该检验方式对应的上限 | `S × (1.4 + 0.25 × level)`，上限 365 天 |
| 失败 | 压到该方式之下 | `S × 0.4`，下限 0.5 天 |

连续 3 次失败 → `status = blocked`，并返回止损建议。
下次通过会自动解除 blocked。

### 检验方式 → 可证明的上限

`recall`→1，`judge`→2，`predict`→3，`debug`→3，`transfer`→4。

**只有 `transfer` 能把 level 推到 4。**这条映射是掌握度不虚高的关键：
复述能过不代表会用，而虚高的掌握度会让 `probe` 该探的不探。

---

## 命令全表

所有操作走 `scripts/learndb.py`。**不要手写 SQL 改库**——schema 靠这层守着。

| 命令 | 用途 |
|---|---|
| `init` | 创建/升级数据库 |
| `status` | 总览：档案、规模、领域、未解决误区。每次会话开始跑 |
| `context <topic>` | **按主题切上下文包**：档案 + 相关 atom + 误区 + 生效策略 |
| `find <query>` | 模糊定位（中文可用）。按需复习的入口 |
| `get <slug>` | 单个 atom 详情 + 前置 + 下游 + 误区 |
| `probe <slug>` | 教之前列出值得探针的**直接前置** |
| `plan <slug> [--gaps-only]` | 全部前置闭包，拓扑序（最深祖先在前） |
| `add <slug> <name> [--prereq ...]` | 新增/更新 atom |
| `verify <slug> --result --method [--phrasing]` | 记检验结果，自动更新 level/stability |
| `set <slug> k=v ...` | 直接改字段 |
| `edge add\|rm <from> <to> [--type]` | 维护依赖图 |
| `misconception add\|list\|resolve` | 误区记录 |
| `pref add\|list\|hit\|prune` | 教学策略进化 |
| `due [--topic]` | 正在衰减的 atom，**仅用户主动要求复习时用** |
| `session start\|close` | 会话记账 |
| `profile --set k=v` | 学习者档案 |
| `export [--path]` | 生成 knowledge.md |
| `backup` | 快照，保留最近 20 份 |
| `goal add\|list\|get\|set\|roadmap` | 大目标增删查；`roadmap` 输出完整树状总览带各阶段进度 |
| `phase add\|list\|get\|set\|start\|close` | 里程碑；`start`/`close` 会自动推进"近详远粗"的展开状态 |
| `project add\|list\|get\|set\|done` | 阶段内的巩固项目；`done` 列出关联 atom 提醒逐个 verify |
| `doctor [--repair-fts]` | 数据完整性体检：悬空边、孤儿项目、重复 seq、FTS 失同步 |

任何命令加 `--help` 看完整参数。

### 返回里的 warning 不要忽略

这个系统最危险的不是报错，是**静默失败**。以下字段出现时必须处理：

| 字段 | 含义 | 不处理的后果 |
|---|---|---|
| `unknown_refs`（`add`） | `--prereq`/`--related` 指向还不存在的 atom | 拼错的话，探针对这个前置**永久空转** |
| `dangling_prereqs`（`probe`） | 依赖边指向不存在的 atom | 同上，且这次探针少探了一项 |
| `warning`（`project add`） | 缺 `--scaffold` / `--user-scope` | 用户不知道哪部分要自己写 |

自顶向下建依赖图时"先连边、后建 atom"是合法用法，所以 `unknown_refs`
只警告不拒绝——但**如果是 slug 拼错了，必须当场 `edge rm` 掉**。

---

## 引用完整性

表结构里**没有外键约束**：给已有表加 `REFERENCES` 需要在 SQLite 里重建整张表，
代价大于收益。改为两层保证：

1. **写入时校验**——硬性错误直接拒绝（`project add` 到不存在的 phase、
   `phase add` 用了重复的 `seq`、phase 命令传了非数字 id）
2. **`doctor` 兜底**——定期体检下面这些，每条都带 `fix` 建议：
   悬空依赖边 / 孤儿项目 / 孤儿阶段 / 重复的 `phase.seq` /
   `atoms.phase_id` 指向不存在的阶段 / 一个目标有多个 `current` 阶段 /
   FTS 索引与 `atoms` 数量不符

大改动之后、或者觉得探针和进度显示不对劲时，跑一下 `doctor`。

---

## 上下文预算

学几个月后库会很大。**不要把整个库读进上下文**——注意力被稀释，
教学判断质量会明显下降，这是实打实的效果损失不是洁癖。

正确姿势：

1. 会话开始 `status`（几十行，拿到全局感）
2. 确定主题后 `context <topic>`（只取相关子图）
3. 教具体 atom 时 `get` / `probe`（单点精确查询）

`context --limit` 默认 25 个 atom，需要时可调。

---

## 常见操作范式

**开一条新学习线：**

```bash
learndb.py add linear-map "线性映射" --domain 线性代数 --alias "linear map" --source "MIT 18.06"
learndb.py add matrix-mult "矩阵乘法" --domain 线性代数 --prereq linear-map
learndb.py plan matrix-mult --gaps-only     # 看还缺什么
```

**一次完整的教学记账：**

```bash
learndb.py probe matrix-mult                                    # 教之前
learndb.py verify matrix-mult --result pass --method transfer \
    --phrasing "就是把右边每一列分别做一次线性映射"              # 教之后
```

**用户答错时：**

```bash
learndb.py verify matrix-mult --result fail --method predict
learndb.py misconception add --atom matrix-mult \
    --wrong "以为矩阵乘法就是对应位置相乘" \
    --correction "是行×列的内积，对应位置相乘那个叫 Hadamard 积"
```

**用户说"我忘了那个什么映射的东西"：**

```bash
learndb.py find "映射"        # 多个候选就把 name 列给用户选
learndb.py get linear-map     # 拿到详情和历史误区，先测后讲
```

**开一个跨月大目标（近详远粗）：**

```bash
learndb.py goal add build-gpt "自己实现一个 GPT" --north-star "复现 GPT-2 规模架构" --weekly-hours 10
learndb.py phase add build-gpt "神经网络基础" --seq 1 --objective "能手写反向传播" --est-weeks 4 --detail detailed
learndb.py phase add build-gpt "语言模型基础" --seq 2 --objective "能实现字符级语言模型" --est-weeks 6 --detail detailed
learndb.py phase add build-gpt "Transformer"   --seq 3 --objective "能从零实现自注意力" --est-weeks 8   # 保持 coarse
learndb.py phase start 1                        # 阶段1设为当前，阶段2自动预览展开
learndb.py add backprop "手写反向传播" --domain 神经网络 --phase 1
learndb.py project add 1 two-layer-nn "手写两层全连接网络" \
    --scaffold "训练循环和画图已写好" --user-scope "你写 forward/backward" --atom backprop
```

**阶段结束，滚动校准：**

```bash
learndb.py verify backprop --result pass --method transfer
learndb.py project done two-layer-nn
learndb.py phase close 1 --retro "比预期快"   # 自动重新估算阶段2、3的周数，推进阶段2为当前
learndb.py goal roadmap build-gpt              # 随时能查完整路线图，不靠聊天记忆
```

---

## Schema 迁移

`atoms.phase_id` 和 `goals`/`phases`/`projects`/`project_atoms` 四张表是后加的。
对已经存在的旧库，每次非 `init` 命令都会先跑一次 `migrate(con)`：
用 `PRAGMA table_info` 检查列是否存在，缺了才 `ALTER TABLE`；新表一律
`CREATE TABLE IF NOT EXISTS`，重复执行是安全的。以后再加字段/表，
照这个模式扩展 `migrate()` 即可，不需要用户手动跑迁移脚本。
