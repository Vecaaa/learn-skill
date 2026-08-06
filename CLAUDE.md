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
| `SKILL.md` | Claude，每次触发 | 主循环、硬约束、reference 索引 | 保持 <500 行；已用约 170 |
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

**探针触发条件**（`cmd_probe`）：`level <= 2` 或 `recall < 0.7`。
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

---

## 可调参数

用户会根据真实使用校准这些。它们都是拍出来的初值。

| 参数 | 位置 | 当前值 | 调大/调小的效果 |
|---|---|---|---|
| `PROBE_THRESHOLD` | learndb.py 顶部 | 0.7 | 调高 → 探针更频繁（更保险但更烦） |
| `DUE_THRESHOLD` | learndb.py 顶部 | 0.6 | 调高 → 复习列表更长 |
| 探针的 level 条件 | `cmd_probe` | `level <= 2` | 改成 `<=3` 会显著增加探针频率 |
| stability 增长率 | `cmd_verify` | `× (1.4 + 0.25×level)` | 调大 → 认为记得更久，探针更少 |
| stability 衰减率 | `cmd_verify` | `× 0.4` | 答错后的惩罚力度 |
| 止损阈值 | `cmd_verify` | 连续 3 次失败 | 调小 → 更早放弃 |
| L2 证据门槛 | `cmd_pref` | 2 次 | 调高 → 进化更慢更稳 |
| 规则过期 | `cmd_pref prune` | 90 天 | — |
| 规则总数上限 | evolution.md | 30 条 | 文档约束，未强制 |
| `context --limit` | learndb.py | 25 个 atom | 上下文预算 |
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

1. **探针阈值需要真实校准** —— 0.7 和 `level<=2` 都是拍的。用几周后
   看用户是抱怨"老考我"还是"老以为我会"，往对应方向调。
2. **stability 模型偏简单** —— 目前是单参数指数衰减，没有区分
   难易 atom 的固有遗忘速度。如果发现某些 atom 总是被高估/低估，
   可以加一个 per-atom 的 difficulty 系数（FSRS 的做法）。
3. **`context` 的切片靠 LIKE 匹配 domain/name** —— 跨领域的主题
   （如"机器学习里的线性代数"）可能切不准。必要时加 tag 表。
4. **没有 atom 合并/拆分的命令** —— 长期使用后一定会需要重构依赖图
   （合并两个总是一起出现的 atom，或拆开一个太大的）。
   目前只能 `set` + `edge` 手动搬。
5. **多领域交叉的路线生成未验证** —— 单领域路线跑得通，
   跨领域（如"为了看懂 ML 论文而学线代"）的裁剪策略还只是文档描述。
6. **尚未做 eval** —— skill-creator 支持跑量化评测。用户选择先用真实
   体验迭代。若日后想要客观对比，可以建 `evals/evals.json` 走那套流程。
7. **大目标模式还没有实战验证** —— schema 和命令跑过冒烟测试（见"如何验证改动"），
   但还没有真实教学场景检验"近详远粗"的展开粒度是否合适、项目体量估计是否
   总是超标、0.5–2.0 的校准限幅是否合理。这些都需要用几周真实使用才能校准。
8. **多个大目标并行时的上下文预算未特别处理** —— `status` 会列出所有
   `active` 的目标，如果用户同时开好几条大目标线，这里可能需要加分页/筛选，
   目前没做，等真的出现这个场景再加。
9. **没有"暂停/放弃大目标"的清理流程** —— `goal set status=paused` 能标记，
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
