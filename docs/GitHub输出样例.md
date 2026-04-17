# PR Guardian｜GitHub 输出样例

## 1. 说明

本页基于当前 `GitHubReporter` 实现和 `tests/test_github_reporter.py` 中的断言，整理三通道输出样例。

目标不是展示“评论长什么样”，而是展示：

- 同一组 Findings 如何映射到 GitHub 三个输出通道
- 什么情况下会进入 gating
- 为什么需要指纹去重

## 2. 输入 Findings

假设当前规则层产出两条 Finding：

```json
[
  {
    "rule_id": "security/secrets-scan",
    "severity": "error",
    "title": "规则标题",
    "message": "发现问题",
    "evidence": [{"file": "src/auth.py", "line": 11, "snippet": "bad()"}]
  },
  {
    "rule_id": "ci/min-permissions",
    "severity": "warning",
    "title": "规则标题",
    "message": "发现问题",
    "evidence": [{"file": ".github/workflows/ci.yml", "line": 4, "snippet": "bad()"}]
  }
]
```

## 3. Check Run 输出

Reporter 会先聚合严重级，然后创建 Check Run。

示意 payload：

```json
{
  "name": "PR Guardian Results",
  "head_sha": "head-sha",
  "status": "completed",
  "conclusion": "failure",
  "output": {
    "title": "PR Guardian Results",
    "summary": "Total findings: 2\nErrors: 1\nWarnings: 1\nInfo: 0"
  },
  "annotations": [
    {
      "path": "src/auth.py",
      "start_line": 11,
      "end_line": 11,
      "annotation_level": "failure",
      "title": "规则标题",
      "message": "发现问题"
    },
    {
      "path": ".github/workflows/ci.yml",
      "start_line": 4,
      "end_line": 4,
      "annotation_level": "warning",
      "title": "规则标题",
      "message": "发现问题"
    }
  ]
}
```

如果存在 `error` 级 Finding，`conclusion` 就会是 `failure`。  
这就是它能做质量门禁的直接原因。

## 4. 行内评论输出

Reporter 会把带 `evidence.file + line` 的 Finding 转成行内评论：

```json
{
  "pr_number": 7,
  "commit_id": "commit-sha",
  "body": "PR Guardian inline findings",
  "event": "COMMENT",
  "comments": [
    {
      "path": "src/auth.py",
      "line": 11,
      "side": "RIGHT",
      "body": "### 规则标题\n- Rule: `security/secrets-scan`\n- Severity: `error`\n- Message: 发现问题\n\n<!-- pr-guardian:fingerprint=xxxxxxxxxxxxxxxx -->"
    },
    {
      "path": ".github/workflows/ci.yml",
      "line": 4,
      "side": "RIGHT",
      "body": "### 规则标题\n- Rule: `ci/min-permissions`\n- Severity: `warning`\n- Message: 发现问题\n\n<!-- pr-guardian:fingerprint=yyyyyyyyyyyyyyyy -->"
    }
  ]
}
```

这里最关键的是尾部的 `fingerprint`。

它的作用是：

- 避免同一问题被重复刷评论
- 让同一条 Finding 能和历史输出做去重

## 5. 汇总评论输出

Reporter 还会按类别生成一条汇总评论：

```md
<!-- pr-guardian:summary -->
## PR Guardian Summary
- Total: 2
- Error: 1
- Warning: 1
- Info: 0

## Security
- [ERROR] **规则标题** (`src/auth.py:11`) - 发现问题

## CI
- [WARNING] **规则标题** (`.github/workflows/ci.yml:4`) - 发现问题
```

这条评论适合让 reviewer 快速看整体风险面，而不是只盯着单行注释。

## 6. reporter.report_findings 的聚合返回

`report_findings(...)` 最终会返回三通道结果汇总：

```json
{
  "check_run": {
    "id": 1,
    "conclusion": "failure",
    "annotation_count": 2
  },
  "review": {
    "created": true,
    "comment_count": 2,
    "id": 9
  },
  "summary": {
    "created": true,
    "updated": false,
    "comment_id": 123
  }
}
```

这说明项目不只是“生成 Findings”，而是把 Findings 继续推进到了真实平台输出层。


