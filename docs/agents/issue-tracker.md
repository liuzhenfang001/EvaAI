# Issue tracker: GitHub

本仓库的 issue 与 spec 以 GitHub issue 形式存在，统一用 `gh` CLI 操作。

## 约定

- **建 issue**：`gh issue create --title "..." --body "..."`。多行 body 用 heredoc。
- **读 issue**：`gh issue view <number> --comments`，用 `jq` 过滤评论并顺带取 labels。
- **列 issue**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，可加 `--label` / `--state` 过滤。
- **评论 issue**：`gh issue comment <number> --body "..."`
- **加 / 删标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**：`gh issue close <number> --comment "..."`

仓库从 `git remote -v` 推断；在 clone 内运行时 `gh` 会自动识别。

## PR 是否作为 triage 入口

**PR 作为请求入口：否。**（若本仓库把外部 PR 当作功能请求，改成 `yes`；`/triage` 会读此标记。）

设为 `yes` 时，PR 走与 issue 相同的标签与状态，用对应的 `gh pr` 命令：

- **读 PR**：`gh pr view <number> --comments`，diff 用 `gh pr diff <number>`。
- **列出待 triage 的外部 PR**：`gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`，只保留 `authorAssociation` 为 `CONTRIBUTOR` / `FIRST_TIME_CONTRIBUTOR` / `NONE` 的（剔除 `OWNER`/`MEMBER`/`COLLABORATOR`）。
- **评论 / 加标签 / 关闭**：`gh pr comment`、`gh pr edit --add-label`/`--remove-label`、`gh pr close`。

GitHub 的 issue 与 PR 共用一个编号空间，裸 `#42` 可能两者皆是：先用 `gh pr view 42`，失败再退回 `gh issue view 42`。

## 当 skill 说「发布到 issue tracker」

创建一个 GitHub issue。

## 当 skill 说「取回相关 ticket」

运行 `gh issue view <number> --comments`。

## Wayfinding 操作

供 `/wayfinder` 使用。**map** 是单个 issue，**child** issue 是 ticket。

- **Map**：单个 issue，标签 `wayfinder:map`，body 放 Notes / Decisions-so-far / Fog。`gh issue create --label wayfinder:map`。
- **Child ticket**：作为 GitHub sub-issue 链接到 map（`gh api` 调 sub-issues 端点）。若未启用 sub-issues，则在 map body 里加到 task list，并在 child body 顶部写 `Part of #<map>`。标签：`wayfinder:<type>`（`research`/`prototype`/`grilling`/`task`）。被认领后指派给执行的开发者。
- **阻塞**：用 GitHub 原生 issue dependencies（规范、UI 可见）。加边：`gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`，其中 `<blocker-db-id>` 是 blocker 的数字 **database id**（`gh api repos/<owner>/<repo>/issues/<n> --jq .id`，不是 `#number` 或 `node_id`）。GitHub 报 `issue_dependencies_summary.blocked_by`（仅未关闭的 blocker，是活门）。若 dependencies 不可用，退回在 child body 顶部写 `Blocked by: #<n>, #<n>`。当所有 blocker 关闭后 ticket 解锁。
- **Frontier 查询**：列出 map 的未关闭 child（`gh issue list --state open`，限定 map 的 sub-issues / task list），剔除有未关闭 blocker 的（`issue_dependencies_summary.blocked_by > 0`，或 `Blocked by` 行里有未关闭 issue）或已指派的；map 顺序第一个优先。
- **Claim**：`gh issue edit <n> --add-assignee @me`，本次会话的第一个写操作。
- **Resolve**：`gh issue comment <n> --body "<answer>"`，然后 `gh issue close <n>`，再把 context 指针（gist + 链接）追加到 map 的 Decisions-so-far。
