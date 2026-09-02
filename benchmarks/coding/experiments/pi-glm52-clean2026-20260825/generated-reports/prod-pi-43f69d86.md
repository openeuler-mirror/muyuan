# Benchmark Report: prod-pi-43f69d86

- Experiment: prod-pi
- Benchmark: AI-Coding-Assist
- Case set: _clean2026
- Algorithm: Baseline (v1.0)
- Agent: pi@1.0.0
- Main model: GLM-5.2

## 项目效果综述表（单行）

| 算法名称 | Agent与模型 | 基础/主模型 | Benchmark | Case数 | 主指标名称 | 主指标值 | 总体耗时(h) | 总体Token消耗 | 相对基线收益 |
| --- | --- | --- | --- | ---: | --- | --- | ---: | ---: | --- |
| Baseline | pi｜main=GLM-5.2 | GLM-5.2 | AI-Coding-Assist | 45 | task_success_rate | 35.6% | 0.417952 | 2867643 |  |

## 通用结果总表（关键字段）

| 字段 | 值 |
| --- | --- |
| 运行 ID | prod-pi-43f69d86 |
| 实验名称 | prod-pi |
| 实验时间 | 2026-08-25 |
| 代码版本 | ee7ff7e |
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
| 主指标值 | 35.6% |
| 成功数 | 16 |
| 成功率 | 35.6% |
| 完成数 | 45 |
| 空 Patch 数 | 6 |
| 基础设施错误数 | 0 |
| 总 Token | 2867643 |
| 平均 Token/Case | 63725.4 |
| 平均 Token/成功 Case | 179227.6875 |
| 总墙钟(h)（实际经过） | 0.417952 |
| Agent 累计耗时(h)（并发下会超过墙钟） | 1.220888 |
| 平均耗时/Case(min) | 1.6279 |
| 总 Step 数 | 407 |
| 平均 Step/Case | 9.04 |
| 总模型调用次数 | 403 |
| 总成本(USD估) | 2.867643 |
| 计价口径 | built-in fallback, no rate configured — this is not a price |
| 成功率 95% CI | [23.2%, 50.2%] |
| Case set fingerprint | 9149d02169845dc5 |

## 采样扩展（pass@k）

本次每个 case 只采样 1 次，`pass@1` / `pass@k` / 成功率必然相等，
**无法说明任何采样扩展收益**。需要收益结论请用 `configs/runs/passk.yaml`
（`max_attempts>1` + temperature>0）。

## 成本轴（每 case token 预算 → 可达成功率）

同一准确率下 token 更省的组合更强；总量单一数字会把「便宜解出」和「烧完预算仍失败」混在一起。

| 每 case 预算(token) | 解出 | 成功率 |
| ---: | ---: | ---: |
| 23007 | 6 | 13.3% |
| 42991 | 9 | 20.0% |
| 77140 | 11 | 24.4% |
| 106090 | 15 | 33.3% |
| 559478 | 16 | 35.6% |

## 分层成功率

### by tier

| 分层 | n | 成功 | 成功率 | 95% CI |
| --- | ---: | ---: | ---: | --- |
| unknown | 45 | 16 | 35.6% | [23.2%, 50.2%] |

### by task_type

| 分层 | n | 成功 | 成功率 | 95% CI |
| --- | ---: | ---: | ---: | --- |
| bugfix | 45 | 16 | 35.6% | [23.2%, 50.2%] |

### by difficulty

> 口径：`estimate_difficulty` 的**体量启发式**（行数 + `def test_` 个数），不是校准测出的 `p_hat` 分档。二者共用 `easy`/`hard` 两个词，不可横比。

| 分层 | n | 成功 | 成功率 | 95% CI |
| --- | ---: | ---: | ---: | --- |
| easy | 14 | 3 | 21.4% | [7.6%, 47.6%] |
| hard | 6 | 4 | 66.7% | [30.0%, 90.3%] |
| medium | 25 | 9 | 36.0% | [20.2%, 55.5%] |

## Case 明细

| case_id | tier | passed | infra_error | tokens | wall_s | steps | 测试通过比 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| rev-05e88429bf55fa4d | - | False | False | 205261 | 174.7324 | 17 | 0.20 |
| rev-09f7740f614d3ea9 | - | True | False | 46736 | 60.1357 | 8 | 1.00 |
| rev-0a13d6e43c487375 | - | False | False | 34931 | 41.9193 | 7 | 0.50 |
| rev-0c386957a849ab91 | - | False | False | 56016 | 111.5979 | 8 | 0.43 |
| rev-0f878818dfebd4f3 | - | True | False | 21795 | 40.8032 | 8 | 1.00 |
| rev-10cc572e61227773 | - | True | False | 91869 | 73.0951 | 11 | 1.00 |
| rev-1b77e6e910b9452e | - | True | False | 106090 | 83.5223 | 13 | 1.00 |
| rev-1ef6270942e0e740 | - | False | False | 23317 | 37.1213 | 8 | 0.67 |
| rev-20f1e5a5d9460800 | - | False | False | 13378 | 126.8185 | 3 | 0.33 |
| rev-298ab8b9d9d05d08 | - | True | False | 559478 | 255.8822 | 14 | 1.00 |
| rev-2a048ca51fb7465e | - | True | False | 16457 | 23.9497 | 7 | 1.00 |
| rev-2b813e27ba5902d5 | - | False | False | 10095 | 13.8820 | 5 | 0.62 |
| rev-2c8c411c1642b559 | - | False | False | 52264 | 89.4195 | 10 | 0.83 |
| rev-326ff6ffac10f77e | - | False | False | 59827 | 48.7777 | 11 | 0.86 |
| rev-39cf6aaba64b4c04 | - | False | False | 68872 | 76.4222 | 12 | 0.80 |
| rev-478df768a8d72026 | - | True | False | 20913 | 37.8080 | 6 | 1.00 |
| rev-607e8f191190459d | - | False | False | 21101 | 135.6472 | 5 | 0.50 |
| rev-6b86e3bbf4e8f22b | - | False | False | 18179 | 122.0624 | 3 | 0.60 |
| rev-6e01e26bfba9221a | - | False | False | 54737 | 68.9412 | 14 | 0.83 |
| rev-6f10facedc91ac5a | - | False | False | 91065 | 181.8992 | 9 | 0.67 |
| rev-738db5f8753b49f2 | - | True | False | 23007 | 29.1970 | 8 | 1.00 |
| rev-74ba9c286a152c50 | - | True | False | 92191 | 93.4531 | 13 | 1.00 |
| rev-7ef5353e673af1af | - | True | False | 13811 | 15.7762 | 8 | 1.00 |
| rev-809ac0ba498403fa | - | True | False | 27076 | 25.1130 | 7 | 1.00 |
| rev-85d5e687ce9f7d51 | - | False | False | 23921 | 23.7819 | 8 | 0.83 |
| rev-863adbcb81f3b56f | - | False | False | 77140 | 99.3597 | 12 | 0.50 |
| rev-863e003d8ddf0a78 | - | False | False | 132087 | 209.2022 | 16 | 0.80 |
| rev-89ac3cb4662a4ad1 | - | True | False | 53049 | 59.2916 | 8 | 1.00 |
| rev-8a469aa591bca490 | - | False | False | 25892 | 163.4922 | 7 | 0.20 |
| rev-98478339eba2368e | - | False | False | 94741 | 199.8229 | 13 | 0.83 |
| rev-9ca9c13ad9483bec | - | False | False | 32030 | 43.5886 | 9 | 0.43 |
| rev-9cdd9aa25c711967 | - | False | False | 15920 | 17.9850 | 6 | 0.80 |
| rev-9f27702b16ac2ace | - | False | False | 43275 | 87.1778 | 7 | 0.33 |
| rev-9fbb2b58ef14ed55 | - | False | False | 38471 | 190.5513 | 7 | 0.33 |
| rev-a60dac9e85e808bf | - | False | False | 151066 | 398.0419 | 13 | 0.60 |
| rev-b8152a1f71487e75 | - | True | False | 42991 | 63.5813 | 8 | 1.00 |
| rev-b8d1d015a9c3a458 | - | False | False | 49053 | 97.9825 | 10 | 0.64 |
| rev-bb519c7bf511ac8b | - | False | False | 52378 | 51.4042 | 9 | 0.83 |
| rev-bd78b451615c2a39 | - | False | False | 40039 | 247.3047 | 7 | 0.60 |
| rev-cbfe633860b77795 | - | False | False | 17649 | 22.1159 | 7 | 0.67 |
| rev-d89577c5071bc735 | - | True | False | 100201 | 124.7244 | 11 | 1.00 |
| rev-e61f1fc58b070103 | - | True | False | 22551 | 34.4234 | 10 | 1.00 |
| rev-f3e957f9a6ec89eb | - | False | False | 79311 | 165.2210 | 8 | 0.80 |
| rev-f4c09bd8410f437b | - | True | False | 23602 | 81.1439 | 7 | 1.00 |
| rev-fef49ee571e4af8c | - | False | False | 23810 | 47.0243 | 9 | 0.83 |

## 失败诊断

- 失败 case 数: **29**

### 按失败类别

| 类别 | 数量 |
| --- | ---: |
| 模型推理失败 | 29 |

### Top 原因

| 原因 | 数量 |
| --- | ---: |
| script_exit=1 | 29 |

### 样例

- `rev-05e88429bf55fa4d` [模型推理失败/completed] exit=1
- `rev-0a13d6e43c487375` [模型推理失败/completed] exit=1
- `rev-0c386957a849ab91` [模型推理失败/completed] exit=1
- `rev-1ef6270942e0e740` [模型推理失败/completed] exit=1
- `rev-20f1e5a5d9460800` [模型推理失败/completed] exit=1
- `rev-2b813e27ba5902d5` [模型推理失败/completed] exit=1
- `rev-2c8c411c1642b559` [模型推理失败/completed] exit=1
- `rev-326ff6ffac10f77e` [模型推理失败/completed] exit=1
- `rev-39cf6aaba64b4c04` [模型推理失败/completed] exit=1
- `rev-607e8f191190459d` [模型推理失败/completed] exit=1
