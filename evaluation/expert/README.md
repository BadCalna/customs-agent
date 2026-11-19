# P18 专家 AI 核查离线评估

本目录用于评估 `adapters/engines/expert_check.py` 在四类分项（`elements` / `images` / `tax` / `regulatory`）上的输出准确率、混淆矩阵、`unknown` 覆盖率以及口径红线。

## 两种验证性质（严禁混同）

1. **技术验证（`gold_standard=false`，默认运行 `fixed_cases()`）**
   - 包含 18 条合成技术样本，覆盖四项 `matched` / `conflict` / `unknown`、C-03、C-18、`read_failed != absent`、8 位子目、历史降为参考、内部异常转 `query_failed` 等红线。
   - **只能证明代码组合与口径边界正确，不能证明真实关务准确率达标**；报告 `threshold_verdict.status` 恒为 `pending_expert_gold_standard`。
2. **关务有效性验证（`--gold-suite <path>`，对应 `ExpertGoldSuite`）**
   - 必须提供 `gold_standard: true`、`synthetic: false`、`annotated_by`、`annotated_at`，且每条样本 `synthetic: false`。
   - 只有通过 Jay/关务专家 D-11 金标准评估且 `false_matched_on_unknown == 0` 才能得到 `threshold_verdict.status = "passed"`。

## 可复现运行方式

在工作区根目录执行（显式传入临时 `DJANGO_SECRET_KEY`，禁止使用 `config.settings`）：

```bash
DJANGO_SECRET_KEY='eval-only-secret' python -m evaluation.expert --output evaluation/expert/report.json
```

如已准备 D-11 金标准 JSON：

```bash
DJANGO_SECRET_KEY='eval-only-secret' python -m evaluation.expert --gold-suite /path/to/expert_gold_suite.json
```
