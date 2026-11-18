# P16 固定合成技术评测（非模型质量评测）

## 范围与限制

- 24 个固定、脱敏、合成技术样本，直接构造既有 `CompareInput` / `RecommendOutput` 后调用 `compare_original_checked(payload, hs_source=None, basis=None)`；**不调用真实模型，不调用 PrecheckEngine.recommend，不评测独立归类能力**。
- 覆盖六类结论；关联→缺码→非法原码→资料/证据待核→一致/不一致的复合优先级；同码缺事实、无证据状态、已取得但空证据、证据不可定位/未核实、非法推荐、原码/推荐8位、非ASCII原码；来源查无、失败、版本待核。
- 所有证据明确 `source_type=synthetic`；来源异常通过受控内存 P14 来源注入，无文件税则回退。某条后置逻辑输出 `consistent` 仅表示该合成 DTO 满足比较器条件，**不表示真实归类正确或原码有效**。
- 报告强制 `synthetic=true / technical_only=true / model_not_called=true / quality_gate_pending=true`。技术通过数不能当业务准确率，不提供 pass rate、业务准确率或虚构概率。真实模型集成评测、已复核脱敏业务样本及质量门禁仍待完成。
- 不联网、不访问数据库、不接业务、不持久化业务状态。CLI 仅允许隔离 settings；不会导入业务 config.settings、读取 .env 或生成 secret。

## 运行

在仓库根目录执行（测试专用离线常量不是业务密钥）：

```bash
export DJANGO_SECRET_KEY='p16-offline-test-only'
export DJANGO_SETTINGS_MODULE='evaluation.precheck.settings'
PYTHON='/Users/jaychen/PycharmProjects/customs-agent/.local/customs-agent-dev/.venv-customs/bin/python'
"$PYTHON" -m evaluation.precheck --output evaluation/precheck/report.json
"$PYTHON" -m django test tests.p16.test_evaluation --verbosity 2
```

CLI 可省略 `DJANGO_SETTINGS_MODULE`，默认设置为 `evaluation.precheck.settings`；若显式指定其他模块则在 `django.setup()` 前拒绝，防止业务 settings 读取真实 .env。**DJANGO_SECRET_KEY 必须显式提供**，代码不会生成或提供默认 secret。隔离 settings 使用 dummy DB 后端且无业务 apps；测试会显示跳过默认数据库。

标准输出始终是 JSON。退出码：`0`=全部技术断言通过，`1`=逐例差异/技术执行失败，`2`=配置、样本校验或写报告错误。错误只包含脱敏错误码，不回显异常文本、私有路径或环境值。`--output` 路径由调用者指定，只在运行者明确要求时写入。

## Python API / 严格输入

```python
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'evaluation.precheck.settings'
os.environ['DJANGO_SECRET_KEY'] = 'p16-offline-test-only'
import django
django.setup()
from evaluation.precheck import build_report
report = build_report()          # 24 个固定样本
# report = build_report(cases)   # 非空 list[dict | SyntheticCase]
assert report['summary']['technical_success']
```

`build_report` 是已初始化 Django 环境的库调用，**不会自动修改调用方 settings 或自行 setup**；调用方负责提前使用隔离 settings，若调用方此前已导入业务 settings，库无法撤销那次外部行为。CLI 额外拒绝已配置的非隔离 settings。

自定义样本结构由 `SyntheticCase` 定义：所有层级 `extra=forbid`、严格类型；必需有效非空唯一 ID、描述、`expected_status`、`expected_issue_codes`。嵌套推荐/证据同样拒绝未知字段；`synthetic` 只能为 true。枚举允许其精确字符串值；布尔不接受字符串或数字。即便传入通过 `model_construct` 构造的实例也会重新校验。拒绝空集合、非 list、重复 ID、缺任意六类预期结论；`cases=None` 明确选择默认固定集，而 `cases=[]` 拒绝，防止空集合假成功。

每例精确比较结论和问题码集合，并检查调用方 DTO 不被修改；返回完整后置输出、预期、实际、逐字段 differences、来源问题、模型/规则/契约/样本版本、逐例及汇总输入/输出 SHA-256。运行错误被脱敏记为 `COMPARISON_TECHNICAL_ERROR`，不会变成通过。

## 可复跑交付

随附 `report.json` 为脱敏技术产物，可按上面命令原位重建。无随机数、运行时间戳、机器路径或环境秘密参与报告。来源时钟、上下文、查询日期均为显式固定合成值，不自动使用本机日期。

哈希规范为 SHA-256(UTF-8 canonical JSON)：`sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False`。逐例 input 包含实际 payload、来源模式和显式 basis；逐例 output 为完整比较器输出。顶层 input/output SHA 对按固定顺序的 `{id,input}` / `{id,output}` 数组计算；`cases_sha256` 也覆盖样本预期及说明。测试会逐一重算哈希、比对重跑报告与交付，且通过禁用 dotenv/network/数据库/model 的断言验证隔离边界。
