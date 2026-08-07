# CLAUDE.md — 修改 learn skill 前先读这份

这份文档的存在理由：用户会长期根据真实使用体验迭代这个 skill，
每次新会话你都是冷启动。这里记的是**架构 + 每个决策背后的理由**，
让你不用重新推导，也不会好心改掉一个刻意为之的设计。

---

## 目录

- [30 秒总览](#30-秒总览)
- [⚠️ 大改动后必须做架构复审](#️-大改动后必须做架构复审)
- [文件职责](#文件职责)
- [设计决策与理由（改之前必读）](#设计决策与理由改之前必读)
- [可调参数](#可调参数)
- [改动指引：想改 X，动哪里](#改动指引想改-x动哪里)
- [不要做的事](#不要做的事)
- [如何验证改动](#如何验证改动)
- [已知待办与开放问题](#已知待办与开放问题)
- [变更记录](#变更记录)

---

## 30 秒总览

一个教学 skill，四个模块围绕**知识原子（atom = 能被单独出一道题检验的
最小知识点）**组织：

1. **教学引擎** —— 主循环 `探针 → 讲解 → 检验 → 记账`，卡住时走五级降维梯
2. **信息检索** —— 先判断稳定/易变知识决定要不要联网，抄成熟大纲取交集
3. **记忆层** —— SQLite 存掌握度、依赖图、误区，跨会话持久
4. **自进化** —— 三层隔离（L1 用户模型 / L2 教学策略 / L3 skill 本体）

在这之上还有一层**大目标模式**（`goal → phase → project`），2026-08-03
第二次迭代加入，专门服务"我想学一年做出 X"这类跨月的宽泛目标——
路线图切成里程碑，近详远粗，滚动校准时间估计，每阶段配巩固项目。
日常单点学习完全不涉及这一层，只在触发大目标时才用。

数据在 `~/.claude/learning/`，skill 在 `~/.claude/skills/learn/`。

---

## ⚠️ 大改动后必须做架构复审

**这是用户明确要求的长期机制，不是可选项。**加了新功能、新表、新命令之后，
不要"能跑就交"——功能是一块块补上去的，每次单看都合理，叠在一起就会出现
只有整体审视才看得见的问题。2026-08-03 第一次做这个复审时，在一个"冒烟测试
全绿"的版本里查出了 5 个真 bug，其中一个直接让核心机制（前置探针）失效。

### 什么时候触发

- 新增了表 / 命令 / 顶层概念（比如这次的 `goal → phase → project`）
- 一次会话里改动超过 ~100 行
- 用户报告了任何"行为不符合预期"的问题
- 距上次复审已经过了好几轮迭代

### 怎么做（按这个顺序，别跳）

**1. 先假设有 bug，主动找，不要只跑现有测试。**
现有冒烟测试只覆盖你当初想到的路径。真正的 bug 在**新老功能的交界处**——
新加的字段有没有被老的读取路径考虑到？老的算法有没有在新场景下失效？

具体的找法是列"疑点清单"再逐条**用命令验证**，不要靠读代码下结论：

| 疑点类型 | 这次实际查出来的例子 |
|---|---|
| 写入路径改了状态，读取路径知不知道 | `verify --result fail` 会刷新 `last_verified`，导致 recall 变 1.0，probe 反而跳过刚答错的前置 |
| 同一个概念有没有两处实现 | `atom_view` 有 level<2 归零逻辑，`cmd_get` 的前置列表自己另算一遍，两处结果不一致 |
| 引用完整性 | `--prereq` 拼错 slug 静默建悬空边，JOIN 直接丢掉 → 探针永久空转且无人察觉 |
| 新表的外键有没有人管 | `project add 999` 挂到不存在的 phase，成功返回 |
| 参数类型 | `phase get <goal_slug>` 直接 ValueError 打一屏 traceback |
| 唯一性约束 | `phases.seq` 可重复 → `close` 找 seq+1 时推进到不确定的阶段 |
| 命名和核心原则是否冲突 | `phase_progress` 把 level>=3 叫 `mastered`，但核心原则是只有 level 4 才算真掌握 |
| 导出/备份路径有没有跟上 | `export` 完全不含 goal/phase/project，路线图不在可读快照和 git 备份里 |
| 每条命令都跑的代码有没有变重 | `migrate()` 每次都 executescript 全量 DDL，`meta.schema_version` 写了却从没被读过 |
| 有没有误导性的死代码 | `PRAGMA foreign_keys=ON` 但一个 `REFERENCES` 都没声明 |
| **换了核心公式，依赖它的常数有没有跟着重算** | `recall_prob` 从 `exp(-t/S)` 换成乘方曲线，但 `PROBE_THRESHOLD=0.7` 还是按指数曲线校准的 → 探针触发时刻整整推迟 10.8 倍，核心机制静默失准 |
| **哨兵值和真实值有没有被混为一谈** | 从未验证过的 atom，`recall_prob` 返回 0.0 表示"没有数据"，但 `update_stability` 把它读成"预期已忘光却答对了→大幅加分" → 首次通过的 stability 被系统性高估 |
| **新加的分类/标签字段有没有防漂移机制** | `error_type` 写成 `notation`/`Notation`/`notations` 会把同类记录拆散，`distill` 永远达不到阈值 —— 标签看着都在，抽象层实际已失效，且完全无声 |
| **每次都跑的命令，输出会不会随时间膨胀** | `probe` 每次都带 `teaching_guidance`，其中 `insights` 原本没有条数上限 —— 画像攒到几十条后，每教一个 atom 都要把它们全塞进上下文 |

**2. 检查渐进披露有没有退化。**
SKILL.md 每次触发都会整份进上下文，所以它只该放"每次教学都用得上"的东西。
这次复审时 SKILL.md 已经涨到 293 行，其中 ~90 行是只在少数会话触发的大目标模式——
已下沉到 `references/planning.md`，SKILL.md 回到 204 行。
**判断标准：这段内容是不是每次教学都要用？不是就下沉，在索引表里留触发条件。**

**3. 跑 `doctor` + 全套冒烟测试。**见[如何验证改动](#如何验证改动)。

**4. 把这次复审的发现追加到[变更记录](#变更记录)**，并更新本节的疑点清单
（如果发现了新类型的坑）。下一个冷启动的会话靠这份清单少走弯路。

### 复审时容易犯的错

- ❌ **只读代码不跑命令**——上面 5 个 bug 里有 4 个是跑出来才确认的，
  读代码时都"看起来没问题"
- ❌ **把"测试通过"当成"没有 bug"**——测试只覆盖想到的路径
- ❌ **发现问题就顺手大改架构**——先确认是真 bug（用命令复现），
  再判断是修补丁还是动架构；这次 9 个发现里只有 1 个（SKILL.md 下沉）动了结构
- ❌ **修完不写进变更记录**——下次复审的人会重新踩一遍

## 文件职责

| 文件 | 谁读 | 职责 | 长度约束 |
|---|---|---|---|
| `SKILL.md` | Claude，每次触发 | 主循环、硬约束、reference 索引 | 保持 <500 行；已用 272 |
| `references/pedagogy.md` | 按需 | 讲解协议、降维梯细节、检验题设计 | 有目录 |
| `references/research.md` | 按需 | 检索分层、源优先级、防幻觉、登录 | 有目录 |
| `references/planning.md` | 按需 | 跨月大目标的阶段规划、项目设计、滚动校准 | 有目录 |
| `references/domains.md` | 按需 | 代码/神经网络与机器学习/读论文的领域专项套路（基于外部研究） | 有目录，末尾附来源 |
| `references/memory-schema.md` | 按需 | 表结构、遗忘模型、命令全表 | 有目录 |
| `references/evolution.md` | 按需 | 三层进化、证据门槛、防膨胀 | 有目录 |
| `scripts/learndb.py` | 执行 | **所有**数据库操作的唯一入口 | 单文件，子命令式 |
| `README.md` | 用户 | 面向人的说明 | — |
| `CLAUDE.md` | 你 | 这份 | — |

**渐进披露原则**：SKILL.md 只放"每次都需要"的东西。任何"偶尔才需要"的
细节都该在 references 里，并在 SKILL.md 的索引表里注明触发条件。
往 SKILL.md 里加内容前，先问它是不是每次教学都用得上。

---

## 设计决策与理由（改之前必读）

这些不是随手选的。改之前请理解代价。

### 1. atom 作为统一抽象

**为什么**：最初的构思里教学/检索/数据库/进化是四个并列模块，缺一个公共对象，
必然导致各写各的、数据对不上。atom 让四者共用同一个实体。

**判定标准**（"能不能立刻为它写出一道题"）是刻意的可操作定义，
比"一个概念"这种模糊说法强得多。

### 2. 只有 `transfer` 检验能推到 level 4

**为什么**：掌握度虚高是这类系统的头号退化路径。一旦库里到处是虚假的
"已掌握"，探针就该探的不探，教学建立在流沙上。宁可低估。

**动它的代价**：放宽会让 `probe` 和 `due` 全部失准。

### 3. 前置探针只看直接前置，不向上追溯

**为什么**：用户明确否决了主动复习。探针能被接受的前提是它是**教学决策**
而非复习任务——只探直接前置、一题定生死、成本十秒。往上追一层就变成
"我是来学新东西的，你怎么让我做旧题"，用户会关掉这个功能。

**探针触发条件**（`cmd_probe`）：`level <= 2` 或 `recall < PROBE_THRESHOLD`
（当前 0.9；这个值必须和 `recall_prob` 的曲线一起看——乘方曲线下 R=0.9
恰好解出 t=S，即探针正好在 `stability_days` 天后触发，见决策 19 和第二次复审）。
level 3 且刚验证过的前置**不探**，否则探针退化成每节课前的例行考试。

### 4. 不做主动复习 / 不做课前热身复习

**这是用户的明确决定**，不是遗漏。设计过程中我提过"热身题顺带复习"的方案，
用户选择了纯按需。**不要好心把它加回来。**

按需入口是 `find`（模糊定位）+ `due`（列衰减项），只在用户开口时调用。

### 5. SQLite 而非文件

**为什么**：核心操作全是关系型查询——前置闭包（递归 CTE）、模糊定位（FTS5）、
按回忆概率排序、按主题切片。文件方案在这四件事上都要把整库读进上下文，
既慢又稀释注意力。用户明确说"不考虑实现复杂度，只要效果最好"。

**FTS5 用 `tokenize='trigram'`** 是关键细节：默认的 unicode61 不切中文词，
中文查询几乎全漏。需要 SQLite ≥ 3.34（用户环境 3.37.2 ✓）。

`find` 同时跑 FTS 和 LIKE 再取并集，因为 trigram 要求查询 ≥3 字符。

### 6. 所有库操作走 learndb.py，禁止手写 SQL

**为什么**：模型自由写文件/SQL，schema 几周内一定漂。这层封装是格式稳定性的
唯一保障。加新能力就加子命令，不要绕过。

### 7. 自进化分三层 + L2 需两次证据

**为什么**：直接让模型改 SKILL.md 会导致规则膨胀、互相矛盾、偶发反馈被固化、
且用户无法回滚。三层隔离把绝大多数进化压在 L2（具体、可验证、可单条删除）。

**两次证据门槛**防的是"用户某天心情不好说了句话就变成永久规则"。

**L3（改 SKILL.md）必须用户确认**——这个 skill 的通用规则是用户自己设计的，
模型单方面改写会破坏掌控感。

### 8. 不索取账号密码

硬边界，与文档加密与否无关。用户提过"存密码文档自动填充"的方案，已明确拒绝，
并给出等效替代（持久化浏览器会话 / 粘贴 / 等价公开源 / API token）。
**`sources.jsonl` 站点注册表绝不存凭据**，只存 `needs_login`、`account_hint`
（"学校 SSO"这种提示，不是账号）、`last_ok`。

用户的真实目标是"不用每次重新登录"，这个目标靠会话持久化就达成了。

### 9. "不要问懂了吗"是硬约束不是建议

**为什么**：这是 AI 教学最高频的失败点，且模型天然倾向这么做（礼貌、自然）。
写成硬约束是因为软性建议压不住这个倾向。

### 10. 领域专项套路单独拆成 domains.md，不并进 pedagogy.md

**为什么**：2026-08-03 做了一轮外部研究（学习科学 + 代码教学 + 神经网络/机器学习
教学 + 读论文方法），发现代码、神经网络/ML、读论文这三个领域各自有一套具体、
可执行、且有实证支持的套路（PRIMM、专家读码的自顶向下路径、Karpathy 式代码
优先于数学、Keshav 三遍读论文法等），但这些套路**领域特定**，不该污染
`pedagogy.md` 的通用教学法。渐进披露原则下，只有真的在教这三类领域时才该
加载这份文件。

`pedagogy.md` 里同时补充了三条来自这轮研究的通用发现：worked example 要随
`level` 升高而褪去（不是永远给完整示例）、检验环节的效应量依据（测试效应
g≈0.5–0.6）、以及同一会话教多个同类 atom 时要穿插而非分块排列
（研究里的对比是 63% vs 20%）。这些补充都标了来源，方便日后核实是否过时。

### 11. 大目标是里程碑式滚动规划，不是固定日历

**为什么**：用户最初的构思是"一年 → 4 阶段 → 每阶段 3 个月"这种日历式切分，
提出后我们讨论过这个设计的脆弱之处并达成一致改为里程碑式（用户在
AskUserQuestion 里选了"近详远粗 + 滚动校准"这个方案）：日历是按最初的猜测
时长倒推的，第一阶段真实花的时间几乎必然和猜测不同，写死的日历从那一刻起
就是错的，而错的计划比没有计划更误导人。

所以 `phases.est_weeks` 是**软性估计**，真正的判断依据是 `objective`
（验收标准："到这一步你应该能做什么"），`phase close` 时用实际耗时
（`actual_weeks`）按比例重新校准所有还没开始的阶段——这个比例限制在
0.5–2.0 倍之间（`cmd_phase` 里的 `close` 分支），防止一次异常波动
把后面所有估计打乱。

**同一决定也覆盖了"要不要一次性把全年细化到项目级"**：不要。只有当前阶段和
下一阶段该是 `detail_level='detailed'`，更远的阶段先只给里程碑名 + 验收标准
（`coarse`）。原因和上面一致：远期内容大概率会被前面学到的东西推翻，
提前设计等于白做。`phase start` / `phase close` 会自动把"当前+下一阶段"
这个滚动窗口往前推。

**动它的代价**：如果日后想改回固定日历（比如用户明确要求"就是想要一个
能打印出来贴墙上的一年计划"），可以在 `goal roadmap` 的输出基础上让模型
现算一份日历视图，但**不要把日历写回数据库当真源**——`objective` 和
`est_weeks`/`actual_weeks` 的分工不应该因为想要一份日历视图就被打破。

### 12. 项目复用 `verify` 的 transfer 机制，不另起一套掌握度判定

**为什么**：用户要的"巩固知识的小项目"如果做成一套独立的"项目完成度"，
就会和 atom 的掌握度体系并行存在、互相打架（项目说"完成"了，但对应 atom
的 `level` 没变，教学循环该不该继续教它？）。所以设计成：`project done`
**不自动**给关联 atom 升级，只是打印出关联 atom 列表提醒你——真正的升级
仍然走正常的 `verify --method transfer`，按用户在这个项目里实际表现出来的
水平来判断。项目由此成为 `transfer` 级检验最自然的载体，而不是绕开检验体系
的另一条路。

**脚手架透明是硬性字段但不是硬性校验**：`project add` 的 `--scaffold` 和
`--user-scope` 缺一个就会在返回里带 `warning`，但命令本身不会拒绝创建
（没法用代码校验"内容写得够不够清楚"）。真正的约束力在 SKILL.md 的文字上，
靠你在创建项目时真的想清楚这两点，而不是靠程序拦截。

### 13. `atoms.phase_id` 用可选外键而不是新建一张关联表

**为什么**：一个 atom 通常只属于一个学习计划的一个阶段（不像
`project_atoms` 那样天然多对多），加一个可空的 `phase_id` 列比另建
`atom_phases` 关联表更省一次 JOIN，而且日常单点学习（不涉及 goal）的 atom
这个字段永远是 NULL，语义上也说得通——"不属于任何计划"本来就是合法状态。

### 14. Schema 演进走 `migrate()`，不要求用户手动升级

**为什么**：这次加了 4 张新表 + 1 个新列，用户已有的库（这次会话之前建的
那个 `~/.claude/learning/learn.db`）不会自动有这些结构。与其让用户手动跑
迁移脚本或者重新 `init`（会丢数据），`main()` 在每个非 `init` 命令前都会跑
一次 `migrate(con)`：先用 `PRAGMA table_info` 检查列是否存在、缺了才
`ALTER TABLE`；新表全部 `CREATE TABLE IF NOT EXISTS`，重复执行天然安全。
**以后再加字段/表，照这个模式扩展 `migrate()`，不要引入需要用户介入的
迁移步骤**——这是本来就该长期维持的模式，不是这次的一次性写法。

**加表/加列时记得 `SCHEMA_VERSION += 1`**：`migrate()` 靠 `meta.schema_version`
走快速路径，版本没变就直接返回。忘了加版本号，老库不会升级，新表就是缺的。

### 15. 引用完整性靠命令层 + `doctor`，不靠数据库外键

**为什么**：给已有表加 `REFERENCES` 需要重建整张表（SQLite 不支持直接加 FK），
代价大于收益。所以分两层保证：

- **写入时校验**：硬性的（`project add` 到不存在的 phase、`phase add` 重复 seq）
  直接拒绝；软性的（`add --prereq` 指向还没建的 atom）只警告不拒绝——
  自顶向下建依赖图时"先连边后建 atom"是合法用法
- **`doctor` 命令兜底**：定期体检悬空边、孤儿项目、重复 seq、FTS 失同步等

早期版本 `connect()` 里有 `PRAGMA foreign_keys=ON` 但一个 FK 都没声明，
纯误导——让人以为有保护，实际上 `--prereq` 拼错会静默建一条悬空边，
而 `probe` 用 JOIN 会把它直接丢掉，**探针从此对这个前置永久空转且无人察觉**。
已移除该 PRAGMA 并在注释里写明真实的保障机制。

**任何返回 `warning` / `unknown_refs` / `dangling_prereqs` 的地方都不要忽略**——
这个系统里最危险的不是报错，是静默失败。

### 16. 失败的检验必须让 recall 归零

**为什么**：`verify --result fail` 会把 `last_verified` 刷成当下，
于是 `recall = exp(-0/S) = 1.0`。对一个 level 3 的 atom 来说，失败一次
`transfer` 检验后 level 不变（failing level-4 test 不否定 level-3 能力），
recall 却是满值 → **`probe` 认为这个前置很牢固，直接跳过**。
用户刚刚答错的东西，同一会话内再教依赖它的内容时探针视而不见——
这正好是探针最该抓的情况。

修法是把 recall 的计算收敛到一个函数 `effective_recall()`，
在里面加两条修正：`level < 2` 归零、`fail_streak > 0` 归零。
**任何地方要算 recall 都必须走这个函数，不要各写各的**——
早期 `cmd_get` 就自己另算了一遍，导致同一个 atom 在 `get` 和 `probe` 里
显示两个不同的 recall。

### 17. `teaching_prefs` 需要一个"作废"状态，不能只靠 90 天 prune

**为什么**：2026-08-05 实战中撞见一个真实场景——用户在纯终端跑的 Claude Code
里明确要求"数学符号用 Unicode，不用 LaTeX"（当时的判断依据：终端不渲染
LaTeX），这条被记成了 `scope=global` 的 L2 规则。同一天，用户在一个能渲染
Markdown/KaTeX 的对话界面里要求反过来用 LaTeX。旧规则的判断依据（终端环境）
被错误地写成了全局适用，新指令和旧规则直接打架，而 `prune` 完全帮不上忙——
它只处理 `active=1` 的规则，这几条还没攒够两次证据，压根不是 active。

不作废的话，这类"前提本身错了"的规则会永久留在候选池里，`pref add`
撞到同文本还可能继续累加证据把它转正，把一个已知的错误判断固化成正式规则。

**修法**：给 `teaching_prefs` 加 `retired`/`retired_reason`/`retired_at` 三列
（`SCHEMA_VERSION` 3→3，2026-08-05 这次改的），新增 `pref retire --id --reason`
子命令：置 `active=0` 的同时标 `retired=1` 并写明理由。`cmd_pref` 的 `add`
分支加了一道守卫——撞到 `retired=1` 的同文本规则时拒绝追加证据，报错里带上
作废理由，逼你写一条新规则而不是绕开当初作废它的原因。

**没有做的**：没有加"删除"命令。作废保留审计轨迹（能看到"这条规则存在过、
为什么被废"），删除会丢失这个历史，而这个系统里"静默丢信息"一直是要避免的
（同决策 15 的引用完整性思路——宁可多一条状态字段，不做真删除）。

**顺带修的数据**：库里 4 条围绕这个假设的规则（"数学符号用纯文本不用 LaTeX"
以及两条"用 Unicode 不用 LaTeX"）全部 retire，改记到 `profile.formula_notation`
这个 L1 字段里，且写成了**环境条件判断**（能渲染 Markdown 的界面用 LaTeX，
纯终端用 Unicode），而不是又立一条同样会过期的全局断言。这类"事实取决于当前
运行环境"的偏好，本来就该进 `profile`（L1，直接生效），不该进
`teaching_prefs`（L2，是"对这个用户有效的教学策略"，不是"当前会话的渲染能力
是什么"）——早期把这条记成 L2 教学策略本身就是归类错误，不只是措辞问题。

### 18. `teaching_prefs` 加 `concept_type`，且过期判断不能只信模型的自觉调用

**为什么加 concept_type**：`scope` 回答"这条规则在哪个领域生效"，但很多真正
有用的规律其实和领域无关，是关于**教学情形本身**的——比如"引入一个新符号该
按什么顺序讲"（先给公式再拆符号，类比放最后）这条规律，跟是在学数学、学
数据库、学编程语言完全无关。只按 `scope` 存会把它锁死在第一次发现它的那个
domain 里，下次别的领域碰到同类情形，这条早就验证过的规律不会被检索到，
等于每个领域都要重新踩一次同样的坑。加一个独立于 `scope` 的 `concept_type`
列（`notation`/`comparison`/`process`/`quantifier`/`test-design`……不是
穷举），`pref list --concept-type X` 能跨 domain 拿到同类规律。`scope` 决定
"在哪生效"，`concept_type` 决定"这是关于什么的"，两个维度正交，不要合并。

**为什么改 `prune` 的过期判断逻辑**：原设计要求模型在用上一条 pref 时主动
调 `pref hit --id N` 去记录命中，`prune` 靠 `last_hit` 判断 90 天没用的规则
要不要降级。实战中发现这个命令整场会话从没被调用过——不是这个模型格外
健忘，是**任何写在 SKILL.md 里的纯文字指令，都不可能被任何模型 100% 可靠
执行**，换更强的模型、或者本会话记忆清零重新部署，这个问题都不会消失，因为
问题根源不在"记性好不好"，而在于"这个行为的落实完全依赖模型此刻记得执行
一个跟主线教学任务无关的额外步骤"。

**修法的思路**（不只是给 `prune` 打补丁，是一条可以复用的设计原则）：
**能设计成数据/代码自动推算的行为，就不要设计成依赖模型自觉执行的步骤**。
具体做法：`prune` 除了看 `last_hit`，额外算一个代理信号——`scope` 对应的
领域最近有没有 atom 被 `verify` 过（这个动作因为是教学主循环里每个 atom
都必须做的核心步骤，被可靠执行的概率远高于 `pref hit` 这种旁支记账）。
只要该领域最近仍在被教，就不判定规则过期，即使 `pref hit` 从没被调用过。
这样即便 `pref hit` 永远没人调用（大概率会发生），系统的过期判断依然不会
错误地把还在起作用的规则悄悄剪掉——**用一个更容易被稳定触发的信号，
兜住一个不容易被稳定触发的信号**，而不是指望后者突然变可靠。

**这个原则本身值得记住**：以后再发现"某个行为设计上依赖模型记得做一件事"
时，先问一句"有没有别的、本来就会被稳定写入的数据可以当代理信号"，
而不是先想着把 SKILL.md 里的提醒写得更用力——后者的可靠性上限很低，
不会因为换了更强的模型就变得可信。

### 19. 遗忘曲线从指数换成 FSRS 启发的乘方曲线，并加入独立的 difficulty 状态量

**为什么**：用户对比了几个同类开源项目（如 `claude-tutor` 用 SM-2，`OpenTutor`
扩展 FSRS）后发现，我们最初的 `recall_prob` 是自己拍脑袋写的单参数指数衰减
`exp(-t/S)`，`已知待办` 那节其实早就承认这个模型偏简单。FSRS（现在是 Anki
的默认算法）用的是乘方曲线，在真实复习数据上验证过比指数曲线更准——真实
遗忘早期降得更快、长尾更平缓，指数曲线两段都会系统性偏离。

**做了什么，没做什么**：换成了 `R(t,S) = (1 + t/(9S))^-1`（FSRS 的标准
乘方形式，代入 t=S 得 R=0.9，S 从此有明确含义——"降到90%记忆概率所需天数"）。
同时加了一个独立的 `difficulty`（1~10）状态量，让 stability 的涨跌幅度
感知"这个 atom 本身多难"，并采纳 FSRS 最有价值的洞察之一：**通过检验时的
"意外程度"决定涨幅**——检验发生时估算的回忆概率越低（本来以为快忘了）
结果还是通过，说明记得更牢，涨幅应该更大；反之回忆概率本来就高，通过在
意料之中，信息量小。**没有做的**：没有照搬 FSRS 真实的 17~21 个训练参数——
那些是在数百万条 Anki 复习记录上拟合出来的，个人单用户场景没有这个数据量
去验证，硬套那些精确常数反而可能比合理的保守估计更不可信。`update_difficulty()`
/`update_stability()` 里的系数是结构合理但保守选择的，不是拟合值，这一点
在函数文档字符串里写清楚了，不要误当成"真正的 FSRS"引用。

**Schema**：`atoms` 加 `difficulty REAL DEFAULT 5.0` 列（`SCHEMA_VERSION` 4→5），
`cmd_verify`/`cmd_set`/`atom_view` 相应打通。`effective_recall()` 签名不变——
乘方曲线只改了 `recall_prob` 内部实现，对外的"level<2 或 fail_streak>0 时
归零"这两条决策16的修正逻辑不受影响，回归测试里的①③（探针必须抓到刚失败
的前置、get和probe的recall必须一致）照样验证过。

### 20. 抽象层是一个统一模式，不是三个补丁：`concept_type` / `error_type` / `insight:`

**为什么**：`teaching_prefs` 和 `misconceptions` 存的都是**具体**记录——绑死在
某个 scope 或某个 atom 上，只有查到那个 scope/atom 时才用得上。碰到全新领域、
没有任何现成记录匹配的情形时，这些积累一点忙都帮不上，模型只能凭本能重新试错，
跟第一次遇到这个用户没有区别。**跨了几十次会话的积累，换个领域就归零**，
这是这套记忆机制最根本的天花板。

修法是同一个模式在三处落地，它们是一件事，不要当成三个独立特性去理解或改动：

| 层 | 具体记录 | 抽象标签 | 提炼产物 |
|---|---|---|---|
| 教学策略 | `teaching_prefs.rule` | `concept_type` | `profile["insight:<type>"]` |
| 错误模式 | `misconceptions.wrong_belief` | `error_type` | `profile["insight:<type>"]` |

`scope`/`atom_slug` 回答"在哪生效"，`concept_type`/`error_type` 回答"这是关于
什么的"——两个维度正交。`profile` 是第三层：每次 `context`/`status` 自动加载，
不需要主动查，所以只有提炼进 `profile` 的东西才能在全新情形下提供方向。

**`distill` 为什么是命令而不是一句"记得定期提炼"**：同决策 18。这个命令自己
不做提炼（那是判断性工作，只有模型能做），但它把"该提炼了"变成一个**能被算
出来的状态**——记录数够阈值、且 `profile` 里没有对应画像（或画像的 `updated`
早于最新记录）就报出来，并附上该类型下的全部原始记录。它已挂进完整版
checkpoint，和 `pref prune` 一样有稳定触发点。

**踩到的坑：抽象标签的粒度**。第一次给库里 5 条误区打 `error_type` 时打出了
5 个互不相同的类型，`distill` 一条都触发不了——**如果每条记录都得到一个独一无二
的标签，这个抽象层就等于没建**。判断标准不是"这两条错误看起来像不像"，而是
**"补救策略是不是同一套"**：重新归类后，"期望值被当成典型路径"和"长期时间平均
被当成单个时刻的快照"合并成了 `aggregate-vs-instance`（表面差很远，但补救都是
"把被忽略的那部分算进去"），"∃ 读成 ∀"和"'找不到'读成'不存在'"合并成
`existential-claim`。合并后两类各 2 条、双双达阈值，提炼出了库里最初的两条
`insight:`。

**没有做的**：没有做自动提炼（比如用规则模板拼一句画像）。画像的价值全在
"读懂几条记录之间的共性"这个判断上，模板拼出来的是噪声，而且会挤占每次会话
都要加载的 `profile` 预算。`distill` 的 hint 里明确写了 `pending` 为空时不要硬凑。

### 21. 引导量按 domain 熟练度定；诊断材料挂在 `probe` 上而不是靠指令

**两个问题，一个挂载点**。

**问题一（引导量）**：`pedagogy.md` 原本只有"worked example 随 `level` 褪去"
这一条，判断依据是单个 atom 的 `level`。但新 atom 永远是 level 0，照它判断的话
**每个新知识点都要从最完整的四步模板讲起**，哪怕学习者在这个领域已经很熟。
专家反转效应（Kalyuga）说的正是这件事：同样一段铺垫，图式未建立时是必要支撑，
图式建立后就变成占用工作记忆的冗余，**降低**而非提高理解。实测证据来自读论文
那场会话：讲到后期核心概念时该压缩却仍套完整模板（冗余），而一个关键的支撑性
符号（θ）又被一句话带过（不足）——两次偏差方向相反，根源同一个：没有依据判断
"此刻该给多少"。修法是 `domain_expertise()`：按该 domain 下 level≥3 的 atom
**绝对数量**分三档（<3 novice / 3–6 developing / ≥7 proficient）。用绝对数
而非比例——路线刚建好时大批 atom 挂在 level 0，比例会把很熟的领域误判成新手。

**问题二（诊断先于响应）**：外部研究里反复出现的一条——多数生成式导师把
"认知诊断"和"教学决策"纠缠在一次生成里，看到困惑就直接编解释，头几次往往
文不对题（θ 那次先给类比被拒、再给公式才落地，正是这个模式）。降维梯的
"先定位"只在用户**已经**说没懂之后触发，是被动补救，不是开讲前的主动诊断。

**为什么两者合并到 `probe`**：按决策 18 的原则，"记得先查规律再讲"这种纯文字
指令不可靠。`probe` 是教学循环里每个 atom 都必跑的第一步，是最稳定的挂载点。
所以 `probe` 的返回里多了 `teaching_guidance`：引导量档位 + 已提炼的
`insights` + 按 `concept_type` 分组的生效规则。**诊断材料在开讲前自动摆好，
而不是指望模型记得去查**——这是让"诊断先于响应"真的发生的唯一可靠办法。
`context` 也返回 `domain_expertise`（可能跨多个 domain，各自档位不同）。

**没有改 schema**：纯查询层的组装，`SCHEMA_VERSION` 不变。

### 22. 证据必须能双向流动：`guidance_feedback` 与归因错位

**为什么**：用户问了一个架构性的问题——"数据库记错了（掌握度、教学方法、
用户画像），后续能自我修正吗"。实测逐项验证后发现，这个系统的自我修正能力
**严重不对称**：

| 误判 | 能自我修正吗 |
|---|---|
| 掌握度虚高 | ✅ 探针→答错→降级，但要求 recall 先衰减、且该 atom 是某个正在学的内容的直接前置 |
| 虚高但是叶子节点 | ❌ 无下游 → 探针永远碰不到（实测 60 天 recall 掉到 0.168 仍无人问津） |
| 掌握度虚低 | ❌ 没有任何自动发现机制，只能靠用户开口说"这我早会了" |
| pref / insight 记错 | ❌ 完全不能 |

根因是**所有证据机制都是单向的**：`level` 有 `verify fail` 这个下降通道，
但 `evidence_count`/`hit_count` 只增不减，`insight` 连计数都没有。

**最严重的是归因错位**（实测场景 F）：照一条错误的 pref 教 → 用户连错 3 次 →
`difficulty` 5.0→5.9、atom 被标 `blocked`，而那条规则 `evidence_count=2`、
`active=1`，**毫发无损继续被使用**。失败被 100% 记到知识点头上，0% 记到教法上——
**错误的教法会伪装成"这个知识点很难"**，而且会自我强化（difficulty 升高 →
stability 涨得更慢 → 探针更频繁 → 看起来"确实很难"）。

**修法**：新增 `guidance_feedback` 表，`pref` 和 `insight` 共用（判定逻辑相同，
分开做就是同一概念的两处实现）。`verify` 新增 `--used-pref` / `--used-insight`：
通过记 hit、失败记 miss。**挂在 `verify` 上而不是让模型记得单独调 `pref hit`**
——`verify` 是每个 atom 都必跑的步骤（同决策 18）。pref 累计 miss ≥
`PREF_MISS_LIMIT`(3) 且多于 hit 时**自动降回候选**；insight 不自动改写（L1 画像
该由复核后重写），由 `distill` 报出来。`distill` 同时新增"超过
`INSIGHT_REVIEW_DAYS`(60) 没复核"的检测。

**顺带修的两处**：`atom_view` 加 `never_assessed` 标记——库里 `level 0` 只代表
"没有数据"，不代表用户不会，`probe` 会提示开讲前先花十秒确认，别把已会的东西
从头讲一遍（这是"虚低"能做的最实际的补救，系统无法自动发现虚低，只能提醒别假设）。
`teaching_prefs.hit_count`/`last_hit` 停止写入，历史值由 `migrate()` 搬进新表，
旧列保留只为不破坏老库——避免同一个计数存在两处。

**仍然没有解决的**：叶子节点的虚高（无下游 → 探针结构上够不到，只能靠用户
主动 `due`）；以及 `error_type`/`concept_type` **打错类别**（不是拼写漂移，
`doctor` 查不出来）。这两条记进"已知待办"。

---

## 可调参数

用户会根据真实使用校准这些。它们都是拍出来的初值。

| 参数 | 位置 | 当前值 | 调大/调小的效果 |
|---|---|---|---|
| `PROBE_THRESHOLD` | learndb.py 顶部 | 0.9 | 调高 → 探针更频繁（更保险但更烦）。**必须和 `recall_prob` 的曲线一起调**：当前乘方曲线下 0.9 恰好让探针在 t=S 触发 |
| `DUE_THRESHOLD` | learndb.py 顶部 | 0.8 | 调高 → 复习列表更长。须始终 < `PROBE_THRESHOLD`，否则 due 比探针还激进，语义矛盾 |
| 探针的 level 条件 | `cmd_probe` | `level <= 2` | 改成 `<=3` 会显著增加探针频率 |
| stability 增长/衰减 | `update_stability()` | 见函数内公式（受 difficulty 和检验时的 recall 共同影响） | 调大 → 认为记得更久，探针更少 |
| difficulty 调整幅度 | `update_difficulty()` | 通过 `-0.3-0.15×ml`／失败 `+0.6-0.1×ml` | 影响 stability 涨跌的放大倍数 |
| `FIRST_VERIFY_RECALL` | learndb.py 顶部 | 0.9 | 首次验证时代入的 recall。调低 → 首次通过给更大的 stability 涨幅（0.0 会退回被修掉的那个高估 bug） |
| `MAX_INSIGHTS_IN_GUIDANCE` | learndb.py 顶部 | 8 | `probe` 每次带出的画像条数上限，直接影响每个 atom 的上下文开销 |
| `PREF_MISS_LIMIT` | learndb.py 顶部 | 3 | 一条 pref 累计几次失效就自动降回候选。调小 → 更快淘汰错规则但容易误杀（一次失败可能是内容本身难） |
| `INSIGHT_REVIEW_DAYS` | learndb.py 顶部 | 60 | 画像多久没复核就由 `distill` 提醒重审 |
| 止损阈值 | `cmd_verify` | 连续 3 次失败 | 调小 → 更早放弃 |
| L2 证据门槛 | `cmd_pref` | 2 次 | 调高 → 进化更慢更稳 |
| 规则过期 | `cmd_pref prune` | 90 天 | — |
| 规则总数上限 | evolution.md | 30 条 | 文档约束，未强制 |
| `context --limit` | learndb.py | 25 个 atom | 上下文预算 |
| 引导量档位的分界 | `GUIDANCE_TIERS` | 3 / 7（该 domain 下 level≥3 的 atom 数） | 调低 → 更早进入压缩讲解（更快但可能跳步）；调高 → 更久保持完整四步（更稳但对熟练领域啰嗦） |
| 提炼阈值 | `DISTILL_MIN_PREFS` / `DISTILL_MIN_MISC` | 3 / 2 | 调低 → 更早提炼画像，但样本少时容易提炼出过度泛化的特征 |
| 单次输出上限 | SKILL.md 硬约束 | 1 atom + 2 术语 | 最可能被用户要求放宽的一条 |
| 阶段时间校准的比值上下限 | `cmd_phase` 的 `close` 分支 | 限制在 0.5–2.0 倍 | 收窄 → 校准更保守；放宽 → 单次异常波动更容易把后面全打乱 |
| 失败后是否让 recall 归零 | `effective_recall()` | `fail_streak > 0` 即归零 | 关掉会让刚答错的前置被探针跳过（决策 16 就是修这个），不建议动 |
| 项目体量的默认建议 | SKILL.md「大目标模式」 | "1-2 次学习会话" | 只是文字建议，没有代码强制；用户反馈项目总是超量时，先检查是不是 `--user-scope` 写得不够窄 |

---

## 改动指引：想改 X，动哪里

| 用户抱怨 | 大概率要改 |
|---|---|
| "话太多了" / "太啰嗦" | SKILL.md 硬约束的篇幅上限；pedagogy.md 讲解协议 |
| "老让我做题" | `cmd_probe` 的触发条件；`PROBE_THRESHOLD` |
| "它老以为我不会" | `cmd_verify` 的 level 提升规则；或探针条件 |
| "它以为我会但我早忘了" | `PROBE_THRESHOLD` 调高；stability 增长率调低 |
| "讲不明白，卡住时没帮上忙" | pedagogy.md 降维梯（L1 定位那步最关键） |
| "学习路线不靠谱" | research.md 的"抄大纲取交集"策略 |
| "每次都要先搜半天" | research.md 第一决策表（稳定 vs 易变） |
| "反馈过的问题下次照犯" | evolution.md 的信号采集表；证据门槛可能太高 |
| "规则越来越乱" | evolution.md 防膨胀；跑 `pref prune` |
| "两条规则互相矛盾" / "这条规则的前提已经不成立了" | `pref retire --id --reason` 作废旧规则（决策 17），别留着和新规则打架，也别手改 `.db` 删掉 |
| 想加新的库能力 | `scripts/learndb.py` 加子命令 + memory-schema.md 命令全表 |
| 触发不准（该用没用/不该用却用了） | SKILL.md frontmatter 的 `description` |
| "路线图怎么每次都要重新问一遍" | 检查是不是漏了 `goal add`/`phase add` 落库这一步（见决策 10 附近的"关键规则"） |
| "项目又变成大工程了" | `--user-scope` 切得不够窄；先砍范围，别延时间（见决策 12） |
| "计划的时间估计完全不准" | 正常——`phase close` 会自动滚动校准，检查是不是漏跑了 `close` |
| 想加大目标之外的新顶层概念 | 先想清楚它和 `goal/phase/project` 是不是真的不同层级，不同再加新表，仿照决策 13 的思路优先考虑加字段而不是加表 |
| "探针老是漏掉该探的东西" | 先跑 `doctor` 看有没有悬空边（拼错的 slug 会让探针永久空转）；再看 `effective_recall()` 的两条修正是否还在 |
| "中文模糊查找搜不到" | `doctor` 会报 `fts_out_of_sync`，跑 `doctor --repair-fts` 重建索引 |

**改 description 时**：它是 skill 是否被调用的唯一依据。当前版本刻意
中英双语并列举了大量口语触发短语，因为模型倾向于"欠触发"。
如果用户说"我说想学 X 它没反应"，往 description 里补更多真实说法。

---

## 不要做的事

- ❌ **不要把主动复习/定时提醒加回来**（用户明确否决过，见决策 4）
- ❌ **不要放宽 transfer→level 4 的映射**（掌握度虚高会连锁污染）
- ❌ **不要让模型直接写 SQL 或手改 .db**（schema 会漂）
- ❌ **不要把 references 的内容并进 SKILL.md**（渐进披露，上下文预算）
- ❌ **不要加任何凭据存储**（硬边界）
- ❌ **不要在 SKILL.md 里堆 ALL CAPS 的 MUST/NEVER**——现有硬约束都带了
  "为什么"，那比命令式更有效。加新约束时也解释理由。
- ❌ **不要自作主张改 L3（SKILL.md 的通用规则）**，先向用户报告和建议
- ❌ **不要把大目标的路线图只停留在对话里不落库**——`goal add`/`phase add`
  是路线图生效的唯一方式，聊天文字不会被下次会话看到（决策 10 的"关键规则"）
- ❌ **不要把 `est_weeks` 当承诺讲给用户**——它是软性参考，`phase close` 后
  会变；讲成"计划就是这样"会在校准后显得自相矛盾
- ❌ **不要让 `project done` 自动升级 atom 掌握度**——过审的是用户实际表现，
  不是"项目状态变成 done 这件事本身"（决策 12）

---

## 如何验证改动

改完 `learndb.py` 后跑一遍冒烟测试（用临时库，不碰用户真实数据）：

```bash
cd ~/.claude/skills/learn/scripts
export LEARN_DB=/tmp/learn-test.db && rm -f $LEARN_DB
python3 learndb.py init
python3 learndb.py add scope "作用域" --domain JS --alias "scope"
python3 learndb.py add closure "闭包" --domain JS --prereq scope
python3 learndb.py verify scope --result pass --method predict
python3 learndb.py probe closure      # scope 刚验证过且 level 3 → 应落在 skip 而非 probe，这是设计使然（见决策 3）
python3 learndb.py find "作用域"      # 中文 FTS 必须命中
python3 learndb.py plan closure
python3 learndb.py context JS
python3 learndb.py verify closure --result fail --method predict
python3 learndb.py misconception add --atom closure --wrong "以为是全局变量" --correction "是词法环境的引用"
python3 learndb.py pref add --rule "先代码后术语" --scope "domain:JS" --evidence t1
python3 learndb.py pref add --rule "先代码后术语" --scope "domain:JS" --evidence t2   # 应转正 active=true
python3 learndb.py export --path /tmp/k.md && python3 learndb.py status
unset LEARN_DB
```

**中文 `find` 必须命中**——trigram 分词器坏了的话这里会静默返回空，
是最容易漏的回归点。

改完 goal/phase/project 相关代码后，额外跑一遍这个（重点看 `phase start`
自动预览下一阶段、`phase close` 自动校准+推进、`status`/`goal roadmap`
里能查到东西）：

```bash
export LEARN_DB=/tmp/learn-goal-test.db && rm -f $LEARN_DB
python3 learndb.py init >/dev/null
python3 learndb.py goal add g1 "测试目标" --north-star "..." --weekly-hours 10
python3 learndb.py phase add g1 "阶段1" --seq 1 --objective "..." --est-weeks 4 --detail detailed
python3 learndb.py phase add g1 "阶段2" --seq 2 --objective "..." --est-weeks 6
PID=$(python3 learndb.py phase list g1 | python3 -c "import json,sys;print(json.load(sys.stdin)[0]['id'])")
python3 learndb.py phase start $PID     # 阶段2的 detail_level 应变成 detailed
python3 learndb.py add a1 "atom1" --phase $PID
python3 learndb.py project add $PID p1 "项目1" --scaffold "..." --user-scope "..." --atom a1
python3 learndb.py verify a1 --result pass --method transfer
python3 learndb.py project done p1
python3 learndb.py phase close $PID --retro "..."   # 应输出校准结果+推进到阶段2
python3 learndb.py goal roadmap g1
python3 learndb.py status | grep -A3 active_goals
unset LEARN_DB && rm -f /tmp/learn-goal-test.db
```

老库（没有 goal/phase/project 表的）跑任意命令时应自动升级 schema，不需要
手动迁移——验证方式是直接对一个旧库跑 `status`，确认 `active_goals` 字段存在
且不报错。

### 回归测试：已修 bug 不能复活

这几条对应决策 15/16 修掉的真 bug，每次改动 `verify`/`probe`/`atom_view`/
依赖图相关代码后必须重跑：

```bash
export LEARN_DB=/tmp/reg.db && rm -f $LEARN_DB
python3 learndb.py init >/dev/null
python3 learndb.py add base "基础" >/dev/null
python3 learndb.py add adv "进阶" --prereq base >/dev/null
python3 learndb.py verify base --result pass --method predict >/dev/null
python3 learndb.py verify base --result fail --method transfer >/dev/null
# ① 刚挂掉检验的前置必须出现在 probe 里，不能在 skip 里
python3 learndb.py probe adv
# ② 拼错的 prereq 必须报 unknown_refs / dangling_prereqs，不能静默
python3 learndb.py add typo "拼错" --prereq nonexistent
python3 learndb.py probe typo
# ③ 挂到不存在的 phase 必须被拒
python3 learndb.py project add 999 ghost "幽灵" --scaffold x --user-scope y
# ④ 传错类型必须是干净报错，不是 traceback
python3 learndb.py phase get some-slug
# ⑤ get 和 probe 的 recall 必须一致（都应为 0.0）
python3 learndb.py get adv
# ⑥ doctor 必须报出那条悬空边
python3 learndb.py doctor
# ⑦ 被 retire 的规则不能靠同文本证据复活
python3 learndb.py pref add --rule "临时规则" --scope "global" --evidence e1
python3 learndb.py pref retire --id 1 --reason "回归测试"
python3 learndb.py pref add --rule "临时规则" --scope "global" --evidence e2   # 应报错，不能悄悄转正
unset LEARN_DB && rm -f /tmp/reg.db
```

`export` 之后还要确认 `knowledge.md` 里有「学习路线图」章节——
路线图不进快照等于 git 备份漏了一整块。

改完 SKILL.md / references 后，最好的验证是**真的用它教一次东西**，
观察是否出现反模式清单上的行为。用户的真实使用反馈比任何自动测试都准。

---

## 已知待办与开放问题

按优先级，用户可能会想动的：

1. **探针阈值需要真实校准** —— 0.9 是从乘方曲线推导出来的（R=0.9 恰好解出 t=S，
   语义干净），但「探针到底该多频繁」本身仍是经验判断，`level<=2` 也还是拍的。用几周后
   看用户是抱怨"老考我"还是"老以为我会"，往对应方向调。
2. ~~stability 模型偏简单~~ —— 2026-08-05 已改成 FSRS 启发的乘方曲线 +
   独立 difficulty 状态量（决策19）。**仍然待办的部分**：`update_difficulty`/
   `update_stability` 里的系数是合理但保守选择的，不是真正 FSRS 那样在
   海量数据上拟合出来的——用几周真实数据看某些 atom 是否被系统性高估/
   低估记忆速度，再校准这几个常数。
3. **叶子节点的虚高探针够不到** —— 没有下游 atom 的知识点，探针结构上永远
   碰不到（实测 60 天 recall 掉到 0.168 仍无人问津），只能靠用户主动说
   "我忘了 X" 走 `due`。要修的话得引入某种周期性抽查，但那会撞上决策 4
   （用户明确否决主动复习），需要先跟用户确认边界。
4. **`error_type`/`concept_type` 打错类别查不出来** —— `doctor` 只能查拼写
   漂移（`notation` vs `Notation`），把 `causal-inversion` 错打成 `quantifier`
   这种分类错误完全无声。可能的方向：`distill` 时让模型复核该类型下的记录
   是否真的同质。
5. **`context` 的切片靠 LIKE 匹配 domain/name** —— 跨领域的主题
   （如"机器学习里的线性代数"）可能切不准。必要时加 tag 表。
6. **没有 atom 合并/拆分的命令** —— 长期使用后一定会需要重构依赖图
   （合并两个总是一起出现的 atom，或拆开一个太大的）。
   目前只能 `set` + `edge` 手动搬。
7. **多领域交叉的路线生成未验证** —— 单领域路线跑得通，
   跨领域（如"为了看懂 ML 论文而学线代"）的裁剪策略还只是文档描述。
8. **尚未做 eval** —— skill-creator 支持跑量化评测。用户选择先用真实
   体验迭代。若日后想要客观对比，可以建 `evals/evals.json` 走那套流程。
9. **大目标模式还没有实战验证** —— schema 和命令跑过冒烟测试（见"如何验证改动"），
   但还没有真实教学场景检验"近详远粗"的展开粒度是否合适、项目体量估计是否
   总是超标、0.5–2.0 的校准限幅是否合理。这些都需要用几周真实使用才能校准。
10. **多个大目标并行时的上下文预算未特别处理** —— `status` 会列出所有
   `active` 的目标，如果用户同时开好几条大目标线，这里可能需要加分页/筛选，
   目前没做，等真的出现这个场景再加。
11. **没有"暂停/放弃大目标"的清理流程** —— `goal set status=paused` 能标记，
   但没有专门的归档/清理命令，长期使用后 `goals` 表里可能积累废弃目标，
   目前手动 `goal set <slug> --field status=paused` 即可，暂不需要更多。

---

## 变更记录

| 日期 | 变更 |
|---|---|
| 2026-08-03 | 初版。四模块设计定稿；SQLite + trigram FTS；探针保留、主动复习否决；三层进化；拒绝凭据存储方案。 |
| 2026-08-03 | 外部研究补丁：新增 `references/domains.md`（代码/神经网络与机器学习/读论文的专项套路）；`pedagogy.md` 补充 worked example 褪去、测试效应效应量、穿插排序（interleaving）三节，均带来源。见决策 10（注：本行的"决策 10"编号后来被下面的大目标模式复用，指的是当时新增的第 10 条，非本次改动后的编号）。 |
| 2026-08-03 | 大目标模式：新增 `goals`/`phases`/`projects`/`project_atoms` 四张表 + `atoms.phase_id` 列，`learndb.py` 新增 `goal`/`phase`/`project` 三组命令，`migrate()` 支持老库无损升级。里程碑用验收标准而非日历、近详远粗、结束时滚动校准时间估计（见决策 11）；项目复用 `verify` 的 transfer 机制而非另起掌握度判定（决策 12）。SKILL.md 新增「大目标模式」整节；`domains.md` 新增"先查现成阶梯式项目课程"一节。路线图强制要求落库，不能只停留在对话里。 |
| 2026-08-03 | **第一次架构复审**（建立了[大改动后必须做架构复审](#️-大改动后必须做架构复审)这个长期机制）。在冒烟测试全绿的版本里查出 5 个真 bug + 4 个架构问题，全部已修：**① 失败的检验被探针无视**（`fail` 刷新 `last_verified` → recall=1.0 → probe 跳过刚答错的前置，核心机制失效，决策 16）；② `--prereq` 拼错静默建悬空边、`probe` 的 JOIN 直接丢掉 → 探针永久空转（决策 15）；③ `project add` 可挂到不存在的 phase；④ `phase get <非数字>` 抛 traceback；⑤ `phases.seq` 可重复 → `close` 推进到不确定阶段。架构侧：⑥ `export` 补上路线图章节（此前 knowledge.md 和 git 备份完全不含 goal/phase/project）；⑦ SKILL.md 293→204 行，大目标模式下沉到 `references/planning.md`，恢复渐进披露；⑧ `migrate()` 改用 `meta.schema_version` 走快速路径（此前每条命令都重跑全量 DDL，且 schema_version 写了从没被读过）；⑨ 移除误导性的 `PRAGMA foreign_keys=ON`（零 FK 声明）。新增 `doctor` 命令做数据完整性体检，`SCHEMA_VERSION=2`。 |
| 2026-08-05 | **教学策略跨领域复用 + 降低对模型自觉性的依赖**（同一场读论文会话的第三轮复盘，用户主动要求结合外部研究分析改进空间）。研究显示自适应 scaffolding 的关键是"选策略前先做状态推断"，而不是攒一堆通用提示语；对照发现三个缺口：① `pref hit` 命令存在但整场会话从未被调用，防膨胀机制形同虚设；② `teaching_prefs.scope` 只有 domain/global 两级，"引入新符号该按什么顺序讲"这类跨领域规律被锁死在发现它的那个 domain 里；③ 多轮卡住后的规律提炼只在 checkpoint 时做，是滞后的。修法：`teaching_prefs` 新增 `concept_type` 列（SCHEMA_VERSION=4），独立于 scope，标记"notation/comparison/process/quantifier/test-design"等教学情形，`pref list --concept-type` 可跨 domain 查询，新增 `pref retag` 给已有规则补标签；`pedagogy.md`"回升是强制的"后补一节，要求爬出 L3+ 降维或反复卡住后当场提炼规律，不等 checkpoint；SKILL.md 硬性约束补一条 `pref hit` 提醒。**更深一层的修法**：用户追问"如果换别的模型跑、或者部署新的 Claude 丢失记忆，怎么保证①不再发生"——认识到纯文字指令本质上无法被任何模型 100% 可靠执行，不管提醒写得多用力都一样；于是把 `pref prune` 的过期判断从**单纯依赖 `last_hit`**（要求模型自觉调用 `pref hit`，不可靠）改为**额外用 scope 对应领域最近是否有 atom 被 verify 过作为兜底代理信号**——只要该领域最近仍在被教，哪怕从没人显式调用过 `pref hit`，规则也不会被误判过期。这是本次复审的核心教训，值得写进决策供以后参考：**优先把"希望发生的行为"设计成数据/代码能自动推算出来的东西，而非依赖模型记得执行的额外步骤**——后者的可靠性上限，不会因为换了更强的模型而变得可信。**同一条原则当场又抓到一个漏网的**：用户追问"90天是怎么设计的，模型又不会到时间自己启动执行"，一查发现 `pref prune` 本身也从没被纳入过 checkpoint 流程——`evolution.md` 提过它属于"定期维护"，但 SKILL.md 的完整版 checkpoint（4.b）里没有这一步，等于这个 90 天检查从设计出来就没有任何稳定会被触发的时机。修法：SKILL.md 完整版 checkpoint 加上 `pref prune`——不是让它变成定时任务（`prune` 本质仍是"被调用那一刻才生效"的惰性检查），而是保证它有一个会被稳定触发的挂载点（每次用户主动要求完整存档时）。 |
| 2026-08-05 | **收尾流程补上随时可触发的 checkpoint**（用户实战反馈：读论文会话中途多次给教学方式反馈，模型嘴上答应"记进 pref"但当场没调用命令，追问后才补上）。根因是 SKILL.md 第4步"收尾"只写了"会话结束时做一次"，隐式信号（用户没直说、模型观察到的模式）只有归纳成 pref 候选才落库，若一直拖到对话被压缩摘要或用户直接关闭，这部分观察会在从未落库的情况下丢失（显式的 `verify`/`misconception add`/`pref add` 本身不受影响，一直是当场写库）。修法：SKILL.md 第4步改为"两种触发缺一不可"——① 模型每教完1-2个atom或对话变长时静默做轻量 `session close`；② 用户随时手动喊"存一下进度"时跑完整版（`session close`+`export`+按门槛写`pref`）。两种触发都不代表学习结束，只是把进度和观察落盘。用户在 [CLAUDE.md 决策7](#7-自进化分三层-l2-需两次证据) 的框架下明确确认了这条 L3 改动。 |
| 2026-08-05 | **checkpoint 的 signals 深度不足**（同一次读论文会话的后续复盘）。上一条改动解决了"何时触发"，但没解决"触发时写什么"——实测中一个知识点（θ 这个符号）内部发生了多轮追问才真正讲懂，但模型当时跑 checkpoint 时只在 `--signals` 里写了一句单轮印象式总结（"用户是刨根问底型"），没有回顾这条多轮追问链条本身、提炼出"公式该排在类比前面讲"这类可复用的结构性规律——这类规律只有在用户事后明确追问"能学到什么"时才被挖出来，说明模型在教学过程中专注于往下讲，容易漏掉需要跨轮对比才能看出的模式。修法：SKILL.md 第4步 checkpoint 部分补一条——写 `--signals` 时如果某 atom 内部有多轮追问/反复卡住，先回顾整条链条再提炼结构性规律，不要只记单轮印象。用户确认为长期规则。 |
| 2026-08-05 | **`teaching_prefs` 新增作废机制**（决策 17）。起因：用户在纯终端 Claude Code 里定的"公式用 Unicode 不用 LaTeX"规则被误记成 `scope=global`，同一天在渲染 Markdown 的对话界面里用户要求相反的行为，暴露出旧规则的判断依据（终端不渲染 LaTeX）被错误地当成了普适事实，且系统没有任何方式作废一条已确认前提有误的规则（`prune` 只处理 `active=1`）。修法：`teaching_prefs` 加 `retired`/`retired_reason`/`retired_at`（`SCHEMA_VERSION` 2→3），新增 `pref retire --id --reason`；`pref add` 撞到已作废的同文本规则会拒绝并提示作废理由，不能靠追加证据复活。作废保留审计轨迹，不做真删除（呼应决策 15 的引用完整性思路）。同时作废了库里 4 条围绕这个错误假设的规则，改记到 `profile.formula_notation`（L1，环境条件判断：能渲染 Markdown 用 LaTeX，纯终端用 Unicode）——这类"事实取决于当前运行环境"的偏好本来就该归 L1 用户模型，不该归 L2 教学策略，早期分类本身就错了。冒烟测试 + 回归测试 + 老库迁移测试（模拟无 `retired` 列的旧库）全部通过，`doctor` 无异常。 |
| 2026-08-05 | **遗忘曲线换成 FSRS 启发的乘方形式 + 独立 difficulty**（决策 19，`SCHEMA_VERSION` 4→5）。用户对比同类开源项目（`claude-tutor` 用 SM-2、`OpenTutor` 扩展 FSRS）后提出。`recall_prob` 从自制的 `exp(-t/S)` 换成 `(1 + t/(9S))^-1`，`S` 从任意衰减常数变成有明确语义的量（记忆概率降到 90% 所需天数）；`atoms` 加 `difficulty`（1~10），`cmd_verify` 改用 `update_difficulty()`/`update_stability()`，采纳 FSRS 的核心洞察——通过检验时的"意外程度"（检验发生时估算的回忆概率越低）决定 stability 涨幅。**明确没做**：没照搬 FSRS 的 17~21 个训练参数（那是数百万条 Anki 记录拟合的，单用户场景无从验证），常数是保守选择并在 docstring 里标注清楚，不要误当成"真正的 FSRS"。回归测试（含决策 16 的 fail-streak 探针检查）+ 老库迁移全过。 |
| 2026-08-05 | **自我修正能力审计 + `guidance_feedback`**（决策 22，`SCHEMA_VERSION` 6→7）。用户提问"数据库记错了之后能不能自我修正"，逐项实测后发现修正能力严重不对称：掌握度虚高能被探针抓（但叶子节点抓不到）、虚低完全没有自动发现机制、pref/insight 记错则毫无下台通道。**核心问题是归因错位**：实测照一条错 pref 教、用户连错 3 次，结果是 atom 的 `difficulty` 5.0→5.9 且被标 `blocked`，而那条规则 `evidence_count=2`/`active=1` 毫发无损——失败 100% 记到知识点头上，错误教法伪装成"内容难"并自我强化（difficulty↑→stability 涨得慢→探针更频繁→看起来确实难）。根因是所有证据单向流动（`evidence_count`/`hit_count` 只增不减，`insight` 连计数都没有）。修法：新增 `guidance_feedback` 表（pref 和 insight 共用，判定逻辑相同、分开做就是两处实现），`verify` 新增 `--used-pref`/`--used-insight` 回报本次用了哪些指导，通过记 hit、失败记 miss；pref 累计 miss≥3 且多于 hit 自动降回候选，insight 由 `distill` 报出来要求复核（L1 画像不该被单条检验静默改写）；`distill` 另加"超 60 天没复核"检测。挂在 `verify` 而非单独的 `pref hit` 上，因为 verify 每个 atom 必跑（同决策 18）。顺带：`atom_view` 加 `never_assessed` 标记 + `probe` 提示（level 0 只代表没数据、不代表不会，别把已会的从头讲）；`teaching_prefs.hit_count` 停止写入、历史值 `migrate()` 搬进新表、旧列保留只为不破坏老库。文档侧同步了 SKILL.md 硬约束里那条与新主路径矛盾的 `pref hit` 指令、memory-schema.md 的命令全表和遗忘模型（还写着旧的指数公式和 0.7/0.6 阈值）。全套冒烟 + 10 项回归 + v6 老库迁移通过。仍未解决、已记进待办：叶子节点虚高（探针结构上够不到，要修会撞上决策 4 否决的主动复习）、标签打错类别（`doctor` 只能查拼写漂移）。 |
| 2026-08-05 | **第二次架构复审**（触发条件：本次会话累计改动 759 行，是 ~100 行阈值的 7 倍）。按流程"先假设有 bug、逐条用命令验证"列了 7 条疑点，**4 条是真 bug、2 条是隐患**，全部已修，并据此给复审疑点清单新增了 4 个类型（见上）。① **`PROBE_THRESHOLD` 语义漂移**（最严重）：`recall_prob` 从指数换成乘方曲线时，没有重算依赖它的 `PROBE_THRESHOLD=0.7`/`DUE_THRESHOLD=0.6`——这两个值是按指数曲线校准的，换曲线后探针触发时刻推迟了 **10.8 倍**（S=2.15 的 atom 从"0.8 天后探"变成"8.2 天后探"），正好命中改动指引里"它以为我会但我早忘了"那条抱怨。修法不是随便调大，而是**让阈值和新曲线的语义对齐**：乘方曲线下 R=0.9 恰好解出 t=S，所以 `PROBE_THRESHOLD=0.9` 让探针正好在 `stability_days` 天后触发，这个字段名从此名副其实；`DUE_THRESHOLD=0.8`（t=2.25S）保持"复习列表比探针晚"的相对关系。② **哨兵值语义混淆**：首次 verify 时 `last_verified` 为空，`recall_prob` 返回 0.0 表示"没有数据"，但 `update_stability` 把低 recall 读作"通过得很意外→记得比预期牢→大幅加 stability"，两种含义混在一起让首次通过的 stability 高估近一倍（2.13 vs 合理的 1.28）。修法：新增 `FIRST_VERIFY_RECALL=0.9`，首次验证时代入它而非 0.0。③ **标签漂移无检测**：`error_type`/`concept_type`/`domain` 写成大小写或单复数不同的变体时，同类记录被拆散、`distill` 永远达不到阈值，而 `doctor` 一声不吭——这是最危险的那种失败（标签看着都在，抽象层已失效）。`doctor` 新增 `tag_drift` 检查（归一化后相同但字面不同即报，带 retag 修复命令）。④ **`export` 漏了新字段**（与第一次复审漏掉路线图同类）：`difficulty`/`error_type`/`concept_type`/引导档位都不在人类可读快照里。⑤ 隐患：`probe` 的 `insights` 无条数上限，而 `probe` 是每个 atom 都跑的命令 → 加 `MAX_INSIGHTS_IN_GUIDANCE=8` 并在超限时提示合并。⑥ 隐患：`distill` 把已 resolve 的误区计入阈值却不标注 → 保留计入（历史证据对提炼画像仍有价值）但在 `source_records` 里加 `resolved` 标记。渐进披露检查：SKILL.md 272 行，未超 500 上限。全套冒烟 + 8 项已修 bug 回归 + 边界用例通过，`doctor` 干净。**另外抓到 6 处文档与代码不一致**（同属静默失效——下一个冷启动的会话会照着过时的数字理解系统）：决策 3 里写死的 `recall < 0.7`、文件职责表里 SKILL.md「已用约 170」（实际 272）、可调参数表里 `PROBE_THRESHOLD`/`DUE_THRESHOLD` 的旧值、以及两条早已被 `update_stability()`/`update_difficulty()` 取代的 stability 增长率/衰减率条目，全部同步；并补上 `FIRST_VERIFY_RECALL`、`MAX_INSIGHTS_IN_GUIDANCE` 这两个新常数。**教训：改常数时顺手 grep 一遍它在文档里的所有出现**——可调参数表尤其容易漏，它是下次想调参的人唯一会看的地方，写着旧值等于主动误导。 |
| 2026-08-05 | **引导量按 domain 熟练度 + 诊断材料挂到 `probe`**（决策 21，无 schema 变更）。这一项把上一轮列出的方向 A 和 ① 合并实现，因为两者是同一件事的两面（"教之前该知道什么"），且共享同一个可靠挂载点。A：新增 `domain_expertise()`，按该 domain 下 level≥3 的 atom 绝对数分三档（<3 novice / 3–6 developing / ≥7 proficient）决定引导总量——原来只按单个 atom 的 `level` 判断，而新 atom 永远是 level 0，导致每个新知识点都从最完整的四步模板讲起，对已有基础的学习者反而降低理解（专家反转效应）。①：新增 `teaching_guidance()`，把引导档位 + 已提炼的 `insights` + 按 `concept_type` 分组的生效规则一次性挂进 `probe` 的返回——`probe` 是教学循环里每个 atom 都必跑的第一步，是最稳定的挂载点，比"记得先查规律再讲"这种文字指令可靠得多（同决策 18）。`context` 也返回 `domain_expertise`（可跨多个 domain）。`pedagogy.md` 新增"引导总量按领域熟练度定"一节，`SKILL.md` 的探针/讲解两步改写。**顺带修掉两个静默失败**：`probe`/`plan` 传一个不存在的 slug 时原本静默返回空列表（看起来像"没有前置要探/要补"，实际可能只是拼错），现在给干净报错——`probe` 的那处还因为这次新增的 `_col(target, "domain")` 变成了崩溃（`None` 抛 `TypeError`，`_col` 原本只捕 `IndexError`/`KeyError`），一并修了。全套回归 + 边界用例（无 domain 的 atom、空 context、不存在的 slug）通过。 |
| 2026-08-05 | **抽象层统一设计：`error_type` + `distill`**（决策 20，`SCHEMA_VERSION` 5→6）。用户要求重点优化"教学"而非"复习"方向，第二轮外部研究（专家反转效应、ITS 的 buggy model/error taxonomy、自我解释元分析）后确定做这一项。核心认识：`teaching_prefs` 和 `misconceptions` 存的都是具体记录，跨领域时归零——这是记忆机制最根本的天花板。修法是同一个模式在三处落地（不是三个独立特性）：`misconceptions` 加 `error_type`（对称于上一轮加的 `concept_type`），新增 `misconception retag` 和 `--error-type` 跨域查询；新增 `distill` 命令，把"该提炼成认知画像了"变成能被算出来的状态（记录数够阈值 + `profile` 无对应 `insight:` 条目或条目已过时），输出附带全部原始记录供模型提炼，已挂进完整版 checkpoint（同决策 18 的原则）。**踩到的坑值得记**：第一次给库里 5 条误区打 `error_type` 打出 5 个互不相同的类型，`distill` 一条都触发不了——抽象标签如果每条记录一个就等于没建，判断标准是"补救策略是不是同一套"而非"看起来像不像"；重新归类合并出 `aggregate-vs-instance` 和 `existential-claim` 两类（各 2 条），提炼出库里最初的两条 `insight:`。`pedagogy.md`/`evolution.md`/`memory-schema.md`/`SKILL.md` 同步更新，全套回归 + v4 老库迁移测试通过。 |
