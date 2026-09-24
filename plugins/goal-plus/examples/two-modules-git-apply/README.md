# Two Modules: Git Apply E2E

这个示例验证真实 Pi 或 Codex 在 Git worktree provider 中同时修改两个 module，经过 frozen
verifier、selection、promotion 和 publication apply 后回写当前 Git root。

## 准备

```bash
cd /path/to/muyuan/plugins/goal-plus
python3 examples/two-modules-git-apply/prepare_workspace.py \
  --destination /tmp/goal-plus-two-modules-git
cd /tmp/goal-plus-two-modules-git
python3 verify.py
```

## 运行

使用 Pi 时启动 `gpt-5.6-luna`、medium thinking：

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

## 使用 Codex

重新安装当前 checkout 后，从 workspace 启动新的 Codex thread，并按安装提示信任 Goal Plus
hooks：

```bash
cd /path/to/muyuan/plugins/goal-plus
./install.sh --codex --yes
cd /tmp/goal-plus-two-modules-git
codex -m gpt-5.6-luna -c model_reasoning_effort=high
```

在 Codex 中粘贴 [codex-goal-prompt.md](codex-goal-prompt.md)。无交互运行也可以直接使用：

```bash
codex exec -C "$PWD" -m gpt-5.6-luna \
  -c 'model_reasoning_effort="high"' - < codex-goal-prompt.md
```

预期结果与 Pi 路径相同：两个 module 均被修改，verifier 得分为 `2`，并生成 publication
commit 回写当前 Git root。
