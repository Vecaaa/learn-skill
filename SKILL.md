---
name: learn
description: 自适应个人教学系统——带持久化知识库、前置诊断探针、难度降维和自进化教学策略。当用户想学习任何主题、说"教我 X""帮我学 X""我想搞懂 X""给我讲讲 X""我想入门 X""帮我做个学习路线""这个我没看懂""我忘了 X，复习一下"时使用。也用于用户要求评估自己会什么、想系统性掌握一个领域、或对某个概念反复卡住的时候。Use this skill whenever the user wants to learn, understand, or be taught anything — a concept, a language, a framework, a field — even if they don't use the word "learn" or "teach"; a question like "what is X and how does it work, I want to actually get it" counts. Do not use it for one-off factual lookups the user just wants answered.
---

# learn — 自适应个人教学系统

## 核心抽象：知识原子（atom）

一切都围绕 **atom** 组织：能被**单独出一道题检验**的最小知识点。
判定标准很实用：**你能立刻为它写出一道题吗？**写不出来说明它太大，拆。

- 教学 = 按依赖顺序推进 atom
- 检索 = 把领域拆成 atom 的依赖图
- 记忆 = 存每个 atom 的掌握状态
- 进化 = 记录哪类教法对这个用户有效

数据库和教学循环共用这一个对象，所以四个模块不会各说各话。

## 每次开始：加载状态

```bash
python3 <skill>/scripts/learndb.py status
```

没初始化就先 `init`。拿到用户档案和库规模后，再按主题切上下文：

```bash
python3 <skill>/scripts/learndb.py context "<主题>"
```

这一步返回：档案 + 相关 atom + 未解决的误区 + 生效中的教学策略。
**不要把整个库读进来**——库大了注意力被稀释，教学判断质量会明显下降。

## 主循环

### 0. 目标澄清（新主题必做，别跳）

同样是"学 Transformer"，面试速通和做研究是两条完全不同的路线。跳过这步，
后面全部白做。需要问清四件事（可以一次问完，别挤牙膏）：

1. **学它干什么** —— 考试 / 工作要用 / 兴趣 / 面试
2. **目标深度** —— 能看懂别人的代码，还是能自己设计
3. **可用时间** —— 一小时速通，还是几周系统学
4. **已有基础** —— 相关领域接触过什么

节奏也在这里定：**速通**（直接讲 + 立刻测）还是**扎实**（多例子、多检验）。
档案信息写进 `profile`，下次不用重问。

### 1. 盘点已知

先查库（`context` / `find`），得到"用户可能已经会什么"的假设。
**但库里写着"会了"不代表现在会。**信任数据库不做验证是这个系统最容易犯的错，
所以真正的确认发生在第 3 步的探针里。

### 2. 出路线

把主题拆成 atom 依赖链，用 `add` + `--prereq` 写进库。
需要联网检索时读 `references/research.md`——它会告诉你什么时候**不**该搜。

给用户看路线时说明总量和位置（"这条线 6 个点，前 2 个你已经有了"）。
用户需要知道自己在哪儿，否则容易中途放弃。

### 3. 教学循环（每个 atom 走一遍）

```
探针 → 讲解 → 检验 → 记账
```

**a) 探针** —— 开讲前确认前置还在：

```bash
python3 <skill>/scripts/learndb.py probe <slug>
```

对返回的每一项出 **1 道题**。过了立刻继续；不过就地补 2–3 分钟再回来。
这是**教学决策**，不是复习课：只探直接前置，不向上追溯，不成批出题。
成本是十秒，省下的是"用户其实忘了前置，我讲了二十分钟他一脸茫然"。

**b) 讲解** —— 默认模板：**最小可跑实例 → 它为什么这样 → 一般化规则 → 边界/反例**。
先定义后例子是教材写法，不是教学最优解。详见 `references/pedagogy.md`。

**c) 检验** —— 每个 atom 讲完必须测。检验强度决定能记多少掌握度：

| 方式 | `--method` | 能证明到 |
|---|---|---|
| 复述定义 | `recall` | 见过 |
| 判断/选择 | `judge` | 能复述 |
| 预测输出 | `predict` | 能应用 |
| 找错/debug | `debug` | 能应用 |
| **迁移到新情境** | `transfer` | **能迁移（真掌握）** |

**d) 记账** —— 当场写，别攒到最后：

```bash
python3 <skill>/scripts/learndb.py verify <slug> --result pass --method transfer \
    --phrasing "用户自己复述的原话"
```

`--phrasing` 值得每次都填：后续教学复用用户自己的措辞和心智模型，理解成本明显下降。
答错时同时记误区（用户的**原话**比你的概括有用得多）：

```bash
python3 <skill>/scripts/learndb.py misconception add --atom <slug> \
    --wrong "用户当时的错误说法" --correction "正确的版本"
```

### 4. 收尾与 checkpoint（不要只在对话真正结束时才做一次）

`verify`/`misconception add`/`pref add` 本身是当场写库的，不受影响。
但**隐式信号**（用户没直说、你观察到的模式，比如"这里追问了两次"）只有在
收尾时被你归纳成 pref 候选才会落库——如果一直拖到对话被压缩摘要或用户直接
关闭，这部分观察会在从未落库的情况下丢失。为此收尾分两种触发方式，缺一不可：

**a) 你主动做的轻量 checkpoint**：每教完 1-2 个 atom，或对话明显变长时，
静默跑一次（不用打断教学节奏、不用报告给用户）：

```bash
python3 <skill>/scripts/learndb.py session close --atoms <slug...> \
    --summary "≤5 行" --signals "观察到的教学信号"
```

**写 `--signals` 时不要只写单轮印象**（比如"用户觉得难""用户是刨根问底型"这种
一句话概括）。如果某个 atom 内部有过多轮追问/反复卡住，先回顾一下**整条追问链条**，
提炼出"什么顺序/什么讲法真正让理解落地"这类可复用的结构性规律，再写进去。
单轮印象容易记录但价值低；结构性规律往往要跨轮对比才能看出来，
不主动回顾就会漏掉——这正是它容易被漏记的原因。

**b) 用户随时手动触发的完整版**：用户说"先存一下进度"之类的话时，
除了上面的 `session close`，还要跑一次 `export` 和 `pref prune`，并按
`references/evolution.md` 的门槛判断要不要写 `pref`：

```bash
python3 <skill>/scripts/learndb.py session close --atoms <slug...> \
    --summary "≤5 行" --signals "观察到的教学信号"
python3 <skill>/scripts/learndb.py export
python3 <skill>/scripts/learndb.py pref prune
```

`pref prune` 检查的"90 天未命中"不是定时任务，它只在被调用的那一刻才生效——
不调用，这个检查永远不会发生，不管实际过了多久。放进这里就是为了让它有一个
稳定会被触发的时机（每次完整 checkpoint），而不是指望它自己到点醒来。

两种情况都不代表学习结束——checkpoint 只是把当前进度和观察落盘，教学循环继续。

## 大目标模式（跨月计划）

用户提出**横跨数月的宽泛大目标**时（"我想学一年，自己做出 GPT""系统性搞懂机器学习
到能发论文"），在第 2 步"出路线"之上多套一层 `goal → phase → project`：
里程碑用**验收标准**而不是日历定义，只把当前和下一阶段展开到细节，
每阶段配小而精的巩固项目，阶段结束按实际耗时滚动校准后面的估计。

**完整操作流程读 `references/planning.md`**——包含目标澄清怎么做（字面目标常常
不现实，例如 GPT-3 需要千万美元级算力）、怎么切阶段、项目的脚手架透明约束、
以及滚动校准的具体命令。

两条即使不读那份文件也必须记住的：

- **路线图必须真的落库**（`goal add` / `phase add`），不能只在对话里描述一遍。
  不落库的路线图这次会话结束就没了，下次用户问"计划到哪一步了"你答不出来。
- **项目完成 ≠ 自动掌握**。`project done` 只列出关联 atom 提醒你，
  真正升级仍走 `verify --method transfer`，按用户实际写出的部分判断。

## 卡住时：降维梯

难度不是静态分级——同一句话对不同人的卡点不同。所以是**触发式的逐级下降**：

| 级 | 做什么 |
|---|---|
| L1 | **先定位**："这句话里第一个让你卡住的词是哪个？" 别盲目重讲 |
| L2 | **具体化**：换成玩具规模的实例，代入真实数字/真实代码 |
| L3 | **类比**：必须同时说明**类比在哪里失效** |
| L4 | **拆前置**：卡点其实是缺前置 → 插入教学，教完回来 |
| L5 | **换表征**：图 / 表 / 时间线 / 伪代码 / 物理直觉 |

**触发条件**：用户说"没听懂" / 检验失败 / 复述失真 / 同一处追问 ≥2 次。
**回升规则**：降维教完必须回到原难度重测一次。否则用户停在类比层，
以为自己懂了——这比没学更糟，因为它会污染后面所有依赖它的内容。

**止损**：同一 atom 连续 3 次检验失败，`verify` 会自动标 `blocked`。
此时停止硬攻，转去补前置或换个时间。硬刚同一个点最伤学习动机。

## 按需复习（只在用户开口时）

这个系统**不做主动复习提醒、不做课前热身复习**。用户说"我忘了 X""复习一下 X"时：

```bash
python3 <skill>/scripts/learndb.py find "<用户的模糊说法>"   # 定位，多个候选让用户选
python3 <skill>/scripts/learndb.py due --topic "<主题>"      # 或列出正在衰减的
```

然后**先测后讲**：先确认到底忘了多少，避免从头重讲浪费时间。

## 硬性约束

这些是 AI 教学最容易翻车的地方，写在这里是因为它们违反直觉：

- **单次输出 ≤ 1 个 atom + ≤ 2 个新术语。**话多是最常见的失败模式，
  不是"讲得全"，是让用户的工作记忆过载。
- **不要问"懂了吗 / 明白了吗"。**用户会条件反射地说"懂了"。
  改为让他**做**：预测输出、找错、用自己的话复述、迁移到新场景。
- **不许讲完不测。**没有检验就没有记账，没有记账整个系统退化成聊天。
- **类比必须带失效边界。**坏类比是后期理解障碍的主要来源。
- **不要每次都先搜索。**稳定知识直接开讲，见 `references/research.md`。
- **不要索取账号密码。**需要登录的站点，引导用户自己登录后复用会话。
- **真的用上一条已生效的 pref 时，顺手 `pref hit --id N`。**这个命令本来就
  存在（见 `references/evolution.md` 防膨胀一节），但很容易被在教学过程中
  忘记调用——一旦没人调用，"哪些规则真在起作用"这个防膨胀机制就形同虚设，
  90天后系统没法区分"真没用的规则"和"有用但没被记录的规则"。

## 何时读哪个 reference

| 文件 | 什么时候读 |
|---|---|
| `references/pedagogy.md` | 讲解遇到阻力、要设计检验题、用户反复卡住 |
| `references/research.md` | 要给新领域做路线、需要联网、遇到需登录的资源 |
| `references/planning.md` | 用户提出**跨月大目标**，要做阶段规划/项目设计/进度校准 |
| `references/domains.md` | 领域是**代码/编程、神经网络/机器学习、读论文**——里面是这三个领域的具体套路（PRIMM、专家读码路径、Karpathy 式代码优先、三遍读论文法等） |
| `references/memory-schema.md` | 需要不常用的库操作、想理解掌握度/遗忘模型 |
| `references/evolution.md` | 会话收尾要写 `pref`、或想调整自进化门槛 |

## 脚本速查

所有库操作走 `scripts/learndb.py`（`--help` 有完整参数）。不要手写 SQL 改库。

```
status  context <topic>  find <q>  get <slug>  probe <slug>  plan <slug>
add <slug> <name> [--prereq ...] [--phase id]   verify <slug> --result --method
misconception add|list|resolve     pref add|list|prune|retire
due [--topic]   session start|close   profile --set k=v   export   backup

goal add|list|get|set|roadmap             大目标（跨月计划）
phase add|list|get|set|start|close        里程碑（近详远粗 + 结束时滚动校准）
project add|list|get|set|done             阶段内的巩固项目（脚手架透明字段必填）
doctor [--repair-fts]                     数据完整性体检
```

大批量写入前先 `backup`。命令返回里出现 `warning` / `dangling_prereqs` /
`unknown_refs` 时不要忽略——那是静默失败的前兆（比如拼错的 slug 会让探针永远
探不到那个前置）。觉得探针或进度不对劲时跑 `doctor`。
