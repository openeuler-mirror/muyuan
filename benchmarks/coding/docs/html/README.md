# docs/html —— 文档站

由 `scripts/build_docs_html.py` 从 `_src/` 下三个手写 HTML 片段构建：

| 页面 | 源片段 | 内容 |
|------|--------|------|
| `index.html` | 脚本内的 `INDEX_BODY` | 导航与流程摘要 |
| `overview.html` | `_src/overview.html` | 背景、流水线、反向构造 |
| `manual.html` | `_src/manual.html` | 环境与端到端步骤（逐步命令以 `docs/USER_GUIDE.md` 为准） |
| `reference.html` | `_src/reference.html` | 设计依据、数据格式、门禁、已发布校准清单 |

```bash
uv run python scripts/build_docs_html.py
```

改站点请改 `_src/*.html` 或 `INDEX_BODY`，然后重建。改 `docs/*.md` 不会改变本站。

CLI 与配置字段的权威文本是 [`docs/REFERENCE.md`](../REFERENCE.md)，不是 `reference.html`。

`_src/` 里还有不参与构建的旧片段（`project-overview.html`、`agentic-scaling-benchmark.html`、`tables.md`），构建脚本会删除已废弃的页面名。
