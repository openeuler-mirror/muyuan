# VLIW Kernel Optimization 任务

为自定义 VLIW/SIMD 机器优化生成的 kernel。

## 任务

修改 `solution.py`，重点优化 `KernelBuilder.build_kernel`，使它为 `problem.py`
定义的机器生成更快的指令程序。

生成程序的最终输出必须与 `reference_kernel2` 完全一致。

主要 workload：

- `forest_height = 10`
- `rounds = 16`
- `batch_size = 256`

## 机器模型

`problem.py` 中的模拟器定义了自定义架构，每个周期具有以下 slot 上限：

- `alu`：12 个 slot
- `valu`：6 个 slot
- `load`：2 个 slot
- `store`：2 个 slot
- `flow`：1 个 slot

SIMD 向量长度是 `VLEN = 8`，机器只有一个 core。指令效果在周期结束时写入，因此同一个
instruction bundle 中的 slot 读取的是旧 scratch 值。

## 必需接口

`solution.py` 必须定义：

```python
class KernelBuilder:
    def build_kernel(
        self,
        forest_height: int,
        n_nodes: int,
        batch_size: int,
        rounds: int,
    ):
        ...
```

runner 会实例化 `KernelBuilder`，调用 `build_kernel`，然后在模拟器上执行
`kb.instrs`。可以在 `solution.py` 内增加 helper method、常量和 scratch 分配逻辑。

## 评分

评分首先要求结果正确，然后比较模拟周期数，越低越好。初始实现在主要 workload 上产生
`147734` 个周期。verifier 报告的参考阈值包括：

- `<147734`
- `<18532`
- `<2164`
- `<1790`
- `<1579`
- `<1548`
- `<1487`
- `<1363`

`cycles` 是案例文件中所有正确 performance case 的最大周期数：

```text
Score = invalid                                任一案例不正确
Score = cycles                                 所有案例都正确
```

错误提交无效，不能参与最佳结果比较。正确时 runner 最后一行输出 `Score: <value>`，
否则输出 `Score: invalid`。

## 本地运行

运行公开 runner：

```bash
python3 runner.py
```

它会读取 `test_cases/public_cases.json`，导入 `solution.py`，再调用 `verifier.py`。
这些公开案例只用于本地正确性和性能演示，不代表官方隐藏评测。

## 约束

- 不要访问网络。
- 不要改变 `problem.py` 的语义。
- 不要修改评测文件。
- 不要为特定随机 seed 或生成实例硬编码答案。
