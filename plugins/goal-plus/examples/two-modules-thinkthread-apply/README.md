# Two Modules: ThinkThread Pi Apply E2E

这个示例验证真实 Pi ThinkThread Root 可以让一个候选同时修改两个 module，经过 frozen
verifier、selection、promotion 和 `fs.apply` 回写 Root。这里的两个 module 是普通快照文件，
不是 Git submodule；ThinkThread 的 nested Git/submodule 能力仍按 provider gate 明确拒绝。

## 准备

```bash
cd /path/to/muyuan/plugins/goal-plus
./install.sh --pi --thinkthread --yes
python3 examples/two-modules-thinkthread-apply/prepare_workspace.py \
  --destination /tmp/goal-plus-two-modules
cd /tmp/goal-plus-two-modules
python3 verify.py
```

初始结果的 `module_score` 是 0、`outcome` 是 `invalid`。只有两个 module 都修改后才会得到
`module_score=2`、`outcome=valid`。

## 运行真实 Pi

使用 `gpt-5.6-luna`、medium thinking 启动 Root：

```bash
tt run --profile goal-plus-pi --fs "$PWD" --write "$PWD" \
  -- --model gpt-5.6-luna --thinking medium
```

在 Root TUI 中粘贴 [pi-goal-prompt.md](pi-goal-prompt.md)。完成后检查：

```bash
python3 verify.py
cat modules/module_a.py modules/module_b.py
```

预期 `module_score=2`、`outcome=valid`、`valid=true`，且两个文件分别为 `VALUE = 7` 和
`VALUE = 11`。
完成后从 `tt ps --forest` 取得 Root ID 并执行 `tt rm <root-id>`。不要提交 workspace、session、
transcript 或 Goal Plus runtime 状态。
