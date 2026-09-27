# Domain Docs

engineering skills 探索代码库时，应如何消费本仓库的领域文档。

## 探索前先读这些

- 根目录 `CONTEXT.md`；或
- 若根目录存在 `CONTEXT-MAP.md`：它指向每个 context 的 `CONTEXT.md`，读与主题相关的那份
- `docs/adr/`：读涉及你即将工作区域的 ADR（multi-context 仓库还要查 `src/<context>/docs/adr/`）

若上述文件不存在，**静默跳过**。不要提示缺失、不要主动建议提前创建。`/domain-modeling` skill（经 `/grill-with-docs` 和 `/improve-codebase-architecture` 到达）会在术语或决策真正落定时懒创建它们。

## 文件结构

single-context（大多数仓库）：

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-xxx.md
│   └── 0002-xxx.md
├── backend/          ← 后端（Python，uv workspace）
└── frontend/         ← 前端（Next.js）
```

multi-context（根目录存在 `CONTEXT-MAP.md` 时）：

```
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← 系统级决策
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← context 级决策
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## 用词汇表词汇

当你的输出命名一个领域概念（issue 标题、重构提案、假设、测试名），用 `CONTEXT.md` 里定义的词，不要漂移到词汇表明确回避的同义词。

若所需概念不在词汇表里，这是信号：要么你在造项目不用的语言（重新考虑），要么确有缺口（记给 `/domain-modeling`）。

## 标记 ADR 冲突

若你的输出与既有 ADR 矛盾，显式指出而非静默覆盖：

> _与 ADR-0007（事件溯源订单）冲突，但值得重新打开，因为……_
