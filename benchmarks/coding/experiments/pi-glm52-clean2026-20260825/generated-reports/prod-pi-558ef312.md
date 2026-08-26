# Benchmark Report: prod-pi-558ef312

- Experiment: prod-pi
- Benchmark: AI-Coding-Assist
- Case set: _clean2026
- Algorithm: Baseline (v1.0)
- Agent: pi@1.0.0
- Main model: GLM-5.2

## 项目效果综述表（单行）

| 算法名称 | Agent与模型 | 基础/主模型 | Benchmark | Case数 | 主指标名称 | 主指标值 | 总体耗时(h) | 总体Token消耗 | 相对基线收益 |
| --- | --- | --- | --- | ---: | --- | --- | ---: | ---: | --- |
| Baseline | pi｜main=GLM-5.2 | GLM-5.2 | AI-Coding-Assist | 45 | task_success_rate | 31.1% | 0.423497 | 2273759 |  |

## 通用结果总表（关键字段）

| 字段 | 值 |
| --- | --- |
| 运行 ID | prod-pi-558ef312 |
| 实验名称 | prod-pi |
| 实验时间 | 2026-08-25 |
| 代码版本 | c839524 |
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
| 空 Patch 数 | 10 |
| 基础设施错误数 | 0 |
| 总 Token | 2273759 |
| 平均 Token/Case | 50528.0 |
| 平均 Token/成功 Case | 162411.35714285713 |
| 总墙钟(h)（实际经过） | 0.423497 |
| Agent 累计耗时(h)（并发下会超过墙钟） | 1.202287 |
| 平均耗时/Case(min) | 1.6030 |
| 总 Step 数 | 386 |
| 平均 Step/Case | 8.58 |
| 总模型调用次数 | 391 |
| 总成本(USD估) | 2.273759 |
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
| 18658 | 4 | 8.9% |
| 35150 | 8 | 17.8% |
| 60835 | 10 | 22.2% |
| 112952 | 12 | 26.7% |
| 221587 | 14 | 31.1% |

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
| hard | 6 | 3 | 50.0% | [18.8%, 81.2%] |
| medium | 25 | 9 | 36.0% | [20.2%, 55.5%] |

## Case 明细

| case_id | tier | passed | infra_error | tokens | wall_s | steps | 测试通过比 |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| rev-05e88429bf55fa4d | - | False | False | 188899 | 119.7245 | 18 | 0.20 |
| rev-09f7740f614d3ea9 | - | True | False | 48290 | 68.5942 | 8 | 1.00 |
| rev-0a13d6e43c487375 | - | False | False | 49688 | 43.0056 | 9 | 0.50 |
| rev-0c386957a849ab91 | - | False | False | 138582 | 179.4961 | 15 | 0.43 |
| rev-0f878818dfebd4f3 | - | True | False | 18493 | 26.9491 | 7 | 1.00 |
| rev-10cc572e61227773 | - | False | False | 60835 | 44.8192 | 9 | 0.80 |
| rev-1b77e6e910b9452e | - | True | False | 112952 | 68.4006 | 13 | 1.00 |
| rev-1ef6270942e0e740 | - | False | False | 18377 | 30.5281 | 7 | 0.83 |
| rev-20f1e5a5d9460800 | - | False | False | 18658 | 148.4488 | 4 | 0.33 |
| rev-298ab8b9d9d05d08 | - | False | False | 71093 | 341.8781 | 9 | 0.50 |
| rev-2a048ca51fb7465e | - | True | False | 20796 | 27.7182 | 8 | 1.00 |
| rev-2b813e27ba5902d5 | - | False | False | 10944 | 14.8511 | 6 | 0.62 |
| rev-2c8c411c1642b559 | - | False | False | 35150 | 140.6911 | 6 | 0.17 |
| rev-326ff6ffac10f77e | - | False | False | 35065 | 39.3230 | 7 | 0.86 |
| rev-39cf6aaba64b4c04 | - | False | False | 63060 | 82.9520 | 11 | 0.80 |
| rev-478df768a8d72026 | - | True | False | 18575 | 28.2852 | 7 | 1.00 |
| rev-607e8f191190459d | - | False | False | 15426 | 117.9839 | 3 | 0.50 |
| rev-6b86e3bbf4e8f22b | - | False | False | 14817 | 108.7174 | 3 | 0.60 |
| rev-6e01e26bfba9221a | - | False | False | 72600 | 327.0805 | 13 | 0.83 |
| rev-6f10facedc91ac5a | - | False | False | 88644 | 148.7530 | 10 | 0.67 |
| rev-738db5f8753b49f2 | - | True | False | 33317 | 47.9161 | 9 | 1.00 |
| rev-74ba9c286a152c50 | - | True | False | 221587 | 213.0603 | 20 | 1.00 |
| rev-7ef5353e673af1af | - | True | False | 11446 | 14.2954 | 7 | 1.00 |
| rev-809ac0ba498403fa | - | True | False | 20501 | 19.0204 | 6 | 1.00 |
| rev-85d5e687ce9f7d51 | - | False | False | 19871 | 27.2718 | 7 | 0.83 |
| rev-863adbcb81f3b56f | - | False | False | 52064 | 105.9794 | 9 | 0.50 |
| rev-863e003d8ddf0a78 | - | False | False | 44514 | 231.4559 | 9 | 0.60 |
| rev-89ac3cb4662a4ad1 | - | True | False | 37704 | 34.8085 | 7 | 1.00 |
| rev-8a469aa591bca490 | - | False | False | 27077 | 115.7361 | 6 | 0.20 |
| rev-98478339eba2368e | - | False | False | 51493 | 87.3685 | 10 | 0.83 |
| rev-9ca9c13ad9483bec | - | False | False | 64026 | 69.1255 | 13 | 0.43 |
| rev-9cdd9aa25c711967 | - | False | False | 13488 | 24.8506 | 6 | 0.80 |
| rev-9f27702b16ac2ace | - | False | False | 59734 | 131.4602 | 7 | 0.33 |
| rev-9fbb2b58ef14ed55 | - | False | False | 35781 | 60.2650 | 8 | 0.50 |
| rev-a60dac9e85e808bf | - | False | False | 25713 | 302.1398 | 7 | 0.20 |
| rev-b8152a1f71487e75 | - | True | False | 145194 | 107.1609 | 16 | 1.00 |
| rev-b8d1d015a9c3a458 | - | False | False | 41830 | 82.3430 | 8 | 0.64 |
| rev-bb519c7bf511ac8b | - | False | False | 47960 | 33.6788 | 8 | 0.83 |
| rev-bd78b451615c2a39 | - | False | False | 17673 | 144.3602 | 5 | 0.60 |
| rev-cbfe633860b77795 | - | False | False | 23436 | 26.5765 | 7 | 0.67 |
| rev-d89577c5071bc735 | - | True | False | 91029 | 94.8122 | 11 | 1.00 |
| rev-e61f1fc58b070103 | - | True | False | 15216 | 22.5074 | 7 | 1.00 |
| rev-f3e957f9a6ec89eb | - | False | False | 17115 | 156.7004 | 3 | 0.20 |
| rev-f4c09bd8410f437b | - | True | False | 31462 | 35.1416 | 8 | 1.00 |
| rev-fef49ee571e4af8c | - | False | False | 23584 | 32.0007 | 9 | 0.67 |

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
- `rev-10cc572e61227773` [模型推理失败/completed] exit=1
- `rev-1ef6270942e0e740` [模型推理失败/completed] exit=1
- `rev-20f1e5a5d9460800` [模型推理失败/completed] exit=1
- `rev-298ab8b9d9d05d08` [模型推理失败/completed] exit=1
- `rev-2b813e27ba5902d5` [模型推理失败/completed] exit=1
- `rev-2c8c411c1642b559` [模型推理失败/completed] exit=1
- `rev-326ff6ffac10f77e` [模型推理失败/completed] exit=1
