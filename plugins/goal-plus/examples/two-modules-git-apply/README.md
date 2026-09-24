# Two Modules: Pi + Git Apply E2E

这个示例验证真实 Pi 在 Git worktree provider 中同时修改两个 module，经过 frozen verifier、
selection、promotion 和 publication apply 后回写当前 Git root。

## 准备

```bash
cd /path/to/muyuan/plugins/goal-plus
python3 examples/two-modules-git-apply/prepare_workspace.py \
  --destination /tmp/goal-plus-two-modules-git
cd /tmp/goal-plus-two-modules-git
python3 verify.py
```

## 运行

使用 `gpt-5.6-luna`、medium thinking 启动 Pi：

```bash
pi --approve --model gpt-5.6-luna --thinking medium
```

在 Pi 中粘贴 [pi-goal-prompt.md](pi-goal-prompt.md)。完成后执行：

```bash
python3 verify.py
git status --short
git log --oneline -2
```

预期 `module_score=2`、`valid=true`、工作树干净，并有 Goal Plus publication commit。
