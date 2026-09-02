# Benchmark Report: prod-pi-5e76551f

- Experiment: prod-pi
- Benchmark: AI-Coding-Assist
- Case set: _clean2026
- Algorithm: Baseline (v1.0)
- Agent: pi@1.0.0
- Main model: GLM-5.2

## 项目效果综述表（单行）

| 算法名称 | Agent与模型 | 基础/主模型 | Benchmark | Case数 | 主指标名称 | 主指标值 | 总体耗时(h) | 总体Token消耗 | 相对基线收益 |
| --- | --- | --- | --- | ---: | --- | --- | ---: | ---: | --- |
| Baseline | pi｜main=GLM-5.2 | GLM-5.2 | AI-Coding-Assist | 45 | task_success_rate | 31.1% | 0.486809 | 2091630 |  |

## 通用结果总表（关键字段）

| 字段 | 值 |
| --- | --- |
| 运行 ID | prod-pi-5e76551f |
| 实验名称 | prod-pi |
| 实验时间 | 2026-08-25 |
| 代码版本 | e227d78 |
| Benchmark 名称 | AI-Coding-Assist |
| Case 集合 | _clean2026 |
| Case 数量 | 45 |
| 有效 Case 数 | 45 |
| 分组口径 | task_type |
| 算法名称 | Baseline |
| 算法版本 | v1.0 |
| 预算轴 | steps |
| 预算值 | max_steps=40, max_wall_time_s=900 |
| 分支数 | 1 |
| 最大 Attempt 数 | 1 |
| 最大 Step 数 | 40 |
| 选择策略 | first-submit |
| Agent 名称 | pi |
| Agent 版本 | 1.0.0 |
| 主模型 | GLM-5.2 |
| 模型组合摘要 | main=GLM-5.2 |
| 采样参数 | temperature=0.0, max_tokens=8192 |
| 评判类型 | 半确定性 |
| 主指标名称 | task_success_rate |
| 主指标值 | 31.1% |
| 成功数 | 14 |
| 成功率 | 31.1% |
| 完成数 | 45 |
| 空 Patch 数 | 9 |
| 基础设施错误数 | 0 |
| 总 Token | 2091630 |
| 平均 Token/Case | 46480.7 |
| 平均 Token/成功 Case | 149402.14285714287 |
| 总墙钟(h)（实际经过） | 0.486809 |
| Agent 累计耗时(h)（并发下会超过墙钟） | 1.426419 |
| 平均耗时/Case(min) | 1.9019 |
| 总 Step 数 | 378 |
| 平均 Step/Case | 8.40 |
| 总模型调用次数 | 371 |
| 总成本(USD估) | 2.09163 |
| 计价口径 | built-in fallback, no rate configured — this is not a price |
| 成功率 95% CI | [19.5%, 45.7%] |
| Case set fingerprint | 9149d02169845dc5 |

## 采样扩展（pass@k）

本次每个 case 只采样 1 次，`pass@1` / `pass@k` / 成功率必然相等，
**无法说明任何采样扩展收益**。需要收益结论请用 `configs/runs/passk.yaml`
（`max_attempts>1` + temperature>0）。

## 成本轴（每 case token 预算 → 可达成功率）

同一准确率下 token 更省的组合更强；总量单一数字会把「便宜解出」和「烧完预算仍失败」混在一起。

| 每 case 预算(token) | 解出 | 成功率 |
| ---: | ---: | ---: |
| 21348 | 3 | 6.7% |
| 32351 | 8 | 17.8% |
| 77231 | 12 | 26.7% |
| 89967 | 13 | 28.9% |
| 129974 | 14 | 31.1% |

## 分层成功率

### by tier

| 分层 | n | 成功 | 成功率 | 95% CI |
| --- | ---: | ---: | ---: | --- |
| unknown | 45 | 14 | 31.1% | [19.5%, 45.7%] |

### by task_type

| 分层 | n | 成功 | 成功率 | 95% CI |
| --- | ---: | ---: | ---: | --- |
| bugfix | 45 | 14 | 31.1% | [19.5%, 45.7%] |

### by difficulty

> 口径：`estimate_difficulty` 的**体量启发式**（行数 + `def test_` 个数），不是校准测出的 `p_hat` 分档。二者共用 `easy`/`hard` 两个词，不可横比。

| 分层 | n | 成功 | 成功率 | 95% CI |
| --- | ---: | ---: | ---: | --- |
| easy | 14 | 2 | 14.3% | [4.0%, 39.9%] |
| hard | 6 | 4 | 66.7% | [30.0%, 90.3%] |
| medium | 25 | 8 | 32.0% | [17.2%, 51.6%] |

## Case 明细

| case_id | tier | passed | infra_error | tokens | wall_s | steps | 测试通过比 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| rev-05e88429bf55fa4d | - | False | False | 25982 | 165.6974 | 4 | 0.20 |
| rev-09f7740f614d3ea9 | - | True | False | 72471 | 69.7727 | 10 | 1.00 |
| rev-0a13d6e43c487375 | - | False | False | 53009 | 49.2801 | 10 | 0.50 |
| rev-0c386957a849ab91 | - | False | False | 91417 | 226.0080 | 9 | 0.86 |
| rev-0f878818dfebd4f3 | - | True | False | 28573 | 37.7652 | 8 | 1.00 |
| rev-10cc572e61227773 | - | True | False | 60863 | 56.9134 | 8 | 1.00 |
| rev-1b77e6e910b9452e | - | True | False | 122074 | 73.8106 | 14 | 1.00 |
| rev-1ef6270942e0e740 | - | False | False | 18452 | 32.3566 | 9 | 0.83 |
| rev-20f1e5a5d9460800 | - | False | False | 77231 | 175.0019 | 10 | 0.67 |
| rev-298ab8b9d9d05d08 | - | False | False | 129974 | 351.3750 | 12 | 0.83 |
| rev-2a048ca51fb7465e | - | True | False | 16706 | 27.8871 | 7 | 1.00 |
| rev-2b813e27ba5902d5 | - | False | False | 10978 | 17.1977 | 6 | 0.62 |
| rev-2c8c411c1642b559 | - | False | False | 78545 | 95.7246 | 14 | 0.83 |
| rev-326ff6ffac10f77e | - | False | False | 46958 | 49.7141 | 10 | 0.86 |
| rev-39cf6aaba64b4c04 | - | False | False | 78693 | 121.4157 | 12 | 0.80 |
| rev-478df768a8d72026 | - | True | False | 14621 | 31.0343 | 6 | 1.00 |
| rev-607e8f191190459d | - | False | False | 13397 | 184.5635 | 3 | 0.50 |
| rev-6b86e3bbf4e8f22b | - | False | False | 78764 | 153.1180 | 9 | 0.80 |
| rev-6e01e26bfba9221a | - | False | False | 52550 | 116.6061 | 12 | 0.83 |
| rev-6f10facedc91ac5a | - | False | False | 23425 | 196.4050 | 6 | 0.50 |
| rev-738db5f8753b49f2 | - | True | False | 36532 | 75.1761 | 10 | 1.00 |
| rev-74ba9c286a152c50 | - | False | False | 21348 | 208.5391 | 5 | 0.25 |
| rev-7ef5353e673af1af | - | True | False | 10190 | 21.0323 | 6 | 1.00 |
| rev-809ac0ba498403fa | - | True | False | 25558 | 27.4188 | 7 | 1.00 |
| rev-85d5e687ce9f7d51 | - | False | False | 22066 | 44.9391 | 7 | 0.83 |
| rev-863adbcb81f3b56f | - | False | False | 110190 | 190.2381 | 14 | 0.50 |
| rev-863e003d8ddf0a78 | - | False | False | 82784 | 391.5487 | 12 | 0.60 |
| rev-89ac3cb4662a4ad1 | - | True | False | 22474 | 34.6526 | 6 | 1.00 |
| rev-8a469aa591bca490 | - | False | False | 24325 | 188.0899 | 6 | 0.20 |
| rev-98478339eba2368e | - | False | False | 89967 | 227.4330 | 12 | 0.83 |
| rev-9ca9c13ad9483bec | - | False | False | 51522 | 118.8226 | 11 | 0.43 |
| rev-9cdd9aa25c711967 | - | False | False | 11395 | 27.8447 | 6 | 0.80 |
| rev-9f27702b16ac2ace | - | False | False | 58178 | 118.0965 | 9 | 0.33 |
| rev-9fbb2b58ef14ed55 | - | False | False | 13261 | 175.0084 | 3 | 0.33 |
| rev-a60dac9e85e808bf | - | False | False | 14541 | 118.1044 | 3 | 0.20 |
| rev-b8152a1f71487e75 | - | True | False | 46759 | 101.8887 | 8 | 1.00 |
| rev-b8d1d015a9c3a458 | - | False | False | 47744 | 77.9467 | 12 | 0.64 |
| rev-bb519c7bf511ac8b | - | False | False | 23125 | 70.7097 | 7 | 0.83 |
| rev-bd78b451615c2a39 | - | False | False | 32351 | 242.9872 | 5 | 0.60 |
| rev-cbfe633860b77795 | - | False | False | 13909 | 28.1999 | 6 | 0.67 |
| rev-d89577c5071bc735 | - | True | False | 77407 | 102.2736 | 11 | 1.00 |
| rev-e61f1fc58b070103 | - | True | False | 25035 | 42.2397 | 11 | 1.00 |
| rev-f3e957f9a6ec89eb | - | False | False | 89146 | 204.3440 | 8 | 0.60 |
| rev-f4c09bd8410f437b | - | True | False | 26786 | 32.0531 | 7 | 1.00 |
| rev-fef49ee571e4af8c | - | False | False | 20354 | 33.8756 | 7 | 0.67 |

## 失败诊断

- 失败 case 数: **31**

### 按失败类别

| 类别 | 数量 |
| --- | ---: |
| 模型推理失败 | 31 |

### Top 原因

| 原因 | 数量 |
| --- | ---: |
| script_exit=1 | 31 |

### 样例

- `rev-05e88429bf55fa4d` [模型推理失败/completed] exit=1
- `rev-0a13d6e43c487375` [模型推理失败/completed] exit=1
- `rev-0c386957a849ab91` [模型推理失败/completed] exit=1
- `rev-1ef6270942e0e740` [模型推理失败/completed] exit=1
- `rev-20f1e5a5d9460800` [模型推理失败/completed] exit=1
- `rev-298ab8b9d9d05d08` [模型推理失败/completed] exit=1
- `rev-2b813e27ba5902d5` [模型推理失败/completed] exit=1
- `rev-2c8c411c1642b559` [模型推理失败/completed] exit=1
- `rev-326ff6ffac10f77e` [模型推理失败/completed] exit=1
- `rev-39cf6aaba64b4c04` [模型推理失败/completed] exit=1
