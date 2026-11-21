# T1 用例实现说明（评测方）

本目录实现 T1「跨形态文件盘点与定向提取」。本说明仅供评测方使用，产品不可见。

---

## ⚠ 能力边界（重要，先读这一节）

**T1 设计上测 Harness，实现上目前只测交付正确性。二者不是一回事。**

| 维度 | T1 应观察什么 | 当前有无原始数据 | 能否判定 |
|---|---|---|---|
| H1 工具执行保真 | 读到的文件内容是否真实落地 | 无（需工具调用日志） | 否 |
| H4 环境状态一致 | 输出字节、编码、换行、路径是否合规 | **有**（`verdict.output_files` 记录 sha256/BOM/换行） | 否（缺 rubric） |
| H7 预算与权限 | 6 个文件是 6 次冗余读还是 1 条批量命令 | 无（需工具调用日志） | 否 |
| H8 过程可追溯 | 工具调用、错误、时间戳能否回放 | 无（需日志） | 否 |

由此产生四条硬结论：

1. 当前 T1 只产出 `overall_pass`，即**完成率**。
2. 完成率是「模型 × Harness 联合指标」（协议 §10.0.1），**不进 Harness 主分、不进主排名**。
3. C1 三款统一模型未落定前（§2.2），完成率**连横向对比都不合法**（§10.0.1）。
4. 因此：**在当前实现下，用 T1 得出任何「Harness 难度 / 能力」结论，在协议框架内都不成立。**

要让 T1 真正测 Harness，须补齐以下前置项（全部属于正式 Run 前的工作，不在本用例实现范围）：

- rubric-v1.0：H1–H8 每子项 0/1/2 锚点 + 正反例（协议 §10.0、待实测清单）
- 工具调用日志采集与解析：H1 / H7 / H8 的原始数据来源
- `workspace_pre_hash` / `workspace_post_hash` 采集：H1 / H4 的环境状态证据
- C1 三款统一模型落定：否则连联合指标也不得横向比较

在这四项落地前，T1 的产出**只能**表述为「某产品在本任务上的交付正确性（完成率 + 内容断言）」，且须标注「模型×Harness 联合指标、未统一模型、不与主排名混排」。

---

## 目录结构
- `TASK.md`：产品任务提示 + 交付契约（五款同一份，双形态输出）。
- `input/`：输入文件包（唯一事实来源，由 build_input.py 生成）。
- `input.zip`：上传形态的确定性打包产物（由 build_input.py 生成）。
- `eval/ground_truth.json`：冻结的期望值——`paths`（路径清单）+ `extract`（5 个键）+ `frozen_at`。
- `eval/inputpkg.py`：输入包遍历与哈希口径的唯一实现，build_input 与 checker 共用。
- `eval/build_input.py`：生成 input/ 文件、校验 ground_truth 冻结路径、确定性打包、生成 input_manifest.json。
- `eval/checker.py`：外部检查器，产出 verdict。
- `eval/test_checker.py`：checker 自测（空/错/对/云端响应 四态）。
- `eval/input_manifest.json`：输入包指纹（content_hash / zip_sha256 / 逐文件 sha256）。
- `eval/task_meta.json`：评分用任务声明（`required_artifacts` / `objective_stop_conditions` / `form` 等），供 `obs.scoring` 读取。
- `runs/`：运行记录与证据目录（每次 Run 追加）。

## 输入包与 ZIP 的对齐（问题 1 锁定）
- 本地形态：`input/` 目录直接放入工作区。
- 上传形态：`input.zip`，ZIP 根即文件本体，无 `input/` 外层目录。
- `path` 列 = 相对「资料包根目录」的路径：本地 `input/data/2024.csv` 与 ZIP 内 `data/2024.csv` 记为同一值 `data/2024.csv`。
- 两形态 path 值一致，checker 用同一份 expected_paths 判定；本地产品若误把工作区当根输出 `input/xxx`，checker 剥掉前导 `input/` 后判等价，不因执行基板差异扣分。

## 生成输入包
```
python eval/build_input.py
```
生成 `input/`、`input.zip`、`eval/input_manifest.json`。幂等，可重复运行。

## 运行检查器
本地输出：
```
python eval/checker.py --input-dir input --output-dir <产品输出目录> [--product <名>]
```
云端响应（响应存成文本文件）：
```
python eval/checker.py --input-dir input --response <响应文本文件> [--product <名>]
```
`--product` 仅透传元数据，不参与判定。退出码 0 = overall_pass，1 = 未通过。
`--manifest` 默认 `eval/input_manifest.json`，用于校验输入包指纹（见下节）。

## 输入完整性与指纹（5b / 5c / 5d）

判定用期望路径**一律取 `ground_truth.json` 的冻结 `paths`**，不再现场 `os.walk` 生成——避免输入被污染后期望跟着漂移。

每次运行检查器都会做三项校验，任一不符即 `input_integrity.pass=false`、`invalid_reason="input_integrity"`、`overall_pass=false`：

| 校验 | 比对象 | 作用 |
|---|---|---|
| 路径集 | 现场输入包 vs `ground_truth.paths` | 防止 input 增删文件后期望与事实脱节（5b） |
| content_hash | 现场复算 vs `input_manifest.json` | 证明「这次判的输入包 = 清单锁定的那一份」（5c） |
| ground_truth 指纹 | 当前 `ground_truth.json` vs 清单记录值 | 发现答案文件在冻结后被改动（5d） |

**注意**：`invalid_reason=input_integrity` 表示输入环境有问题、产品表现不可判定，不应算作产品失败。此项已纳入协议附 A 的「基础设施无效 Run」清单（输入包指纹不符）。

`build_input.py` 在打包时会先校验冻结 paths 与输入包一致，不一致直接退出码 1，从源头阻止期望脱节。

## 输出字节证据（5f）

`verdict.output_files` 记录每个产物的 `sha256 / size / bom_utf8 / newline / decodable_utf8`（云端记录响应文本的哈希）。这些只作证据留档，**不参与判定**——字节级契约归 H4，等 rubric 落地后再判。

加 `--workspace <产品工作区>` 启用部署守卫：若 `ground_truth.json` / `checker.py` 等答案资产出现在工作区内，判 `leak_guard=fail`，overall_pass 强制为 false。

## 判定门禁（三层，全部机械可查）

| 层 | 检查内容 | 是否门禁 |
|---|---|---|
| schema | 表头必须正好是契约列（inventory 3 列 / extract 2 列）；每行字段数与表头一致；format ∈ {json,csv,txt,md,log,other}；encoding ∈ {utf-8,gbk,other}；path/key 不重复；extract 不得有契约外的 key；数值须为十进制定点（禁科学计数法）；候选不唯一（本地多个同名文件 / 云端多个同表头代码块）→ 歧义 | 是 |
| inventory | path 集合与输入包逐一比对（多/少都 fail）；本地误带 `input/` 前缀自动归一 | 是 |
| extract | 5 个键存在且按类型精确匹配（string 精确；number/integer 定点解析后数值相等） | 是 |
| format_encoding | format/encoding 标注是否**正确** | 否（仅记录，问题2 挂起） |

注意区分：schema 只判「值域合法」，H4 才判「标注是否正确」。

## 运行记录字段契约（问题 3 确认）
每次 Run 追加一条记录，至少包含：

- 产品四要素：`product` / `version` / `tier_model` / `eval_date`
- `model_state`：
  - `product_label`（界面显示模型名）
  - `id_visible`（模型 ID 是否可见）
  - `fingerprint`（身份指纹：输出元数据 ID/版本或探针结果）
  - `drift`（与本任务基线指纹是否一致；true → Run 判无效重跑）
  - `fixed_version`（是否固定版本）
  - `temperature_setting` / `temperature_actual`
- `skill_state`：
  - `baseline_bare`（是否裸工具基线）
  - `enabled_list`（本 Run 启用 SKILL 列表，裸基线为空）
  - `auto_match_off`（自动技能匹配是否关闭）
  - `bare_tools_only_verified`（仅默认裸工具是否经行为验证）
- `input_state`：
  - `form`（dir | zip）
  - `content_hash`（排序后「相对路径+\x00+字节」的 SHA-256，跨形态同一，从 input_manifest.json 抄录）
  - `zip_sha256`（云形态）
  - `manifest_sha256`
- 工具调用：`tool_calls_total` / `visible` / `unknown` / `duplicate` / `invalid` / `unnecessary` / `after_timeout` / `subcall_count`
- 时间线/预算：`start_ts` / `end_ts` / `wall_time_s` / `timeout_hit` / `budget_hit` / `terminate_reason` / `completion_at_terminate`
- 人工干预：`semantic_inputs` / `mechanical_continues` / `control_actions` / `session_isolation_ok`
- 安全：`safety_events[]` / `safety_gate`
- 交付正确性：checker 产出的 `verdict`
- 证据：`evidence[]` / `evidence_grade`
- 环境：`vm_snapshot_id` / `workspace_pre_hash` / `workspace_post_hash` / `output_file_hashes`

`model_state` / `skill_state` / `input_state` 的采集脚本本轮未实现，评测方按字段契约手动填写。

## 本轮无法自动判定的内容（诚实清单）
1. `format` / `encoding` 两列的正确性：checker 只采集不判定（问题 2 挂起，本轮不设门禁）。
2. 云端产品解包后的输入包是否与本地字节一致：不可观测；`zip_sha256` 只证明上传的 `input.zip` 打包产物字节，证明不了云端实际看到的（解包后）文件。云端 Run 的 `input_integrity` 因此只能校验本地那份 `input/`，对云端实际输入无约束力。
3. `tool_calls_unknown` 等 Harness 事件：本轮 checker 不解析工具调用日志，需后续轮次接入。
4. **`model_state` / `skill_state` / `input_state` 采集未实现，其中 `skill_state` 缺失导致「测的是 Harness 不是 SKILL」无法验证。**
   三个 state 当前只有字段契约、无采集机制（`input_state` 的 content_hash 可由 build_input.py 直接产出，是三者中唯一机械可得的）。
   最严重的是 `skill_state`：本任务的文件读取/盘点/CSV 生成恰是 SKILL 最容易「整体吞掉」的类型；协议 §5.3 要求裸 SKILL 基线（无额外 SKILL + 仅默认裸工具）作为控制变量，但本轮未采集 `baseline_bare / enabled_list / auto_match_off / bare_tools_only_verified`，故 SKILL 是否介入不可观测。
   此为控制变量执行缺口，须在正式 Run 前补齐（§2.1 预检 + §9 逐 Run 记录），否则结论无法与 SKILL 切割。
5. **H1–H8 打分：rubric-v1.0 锚点未实现，当前无机械可查的 Harness 判据。**
   详见开篇「能力边界」。要点：T1 的三个主要观察点（H1 文件操作 / H4 输出字节 / H7 工具节制）均无判据；5 个 extract 值区分度≈0；因此当前 T1 只能产出完成率，且完成率是联合指标、未统一模型时不得横向比较。

6. **「硬编码 / 读取答案 / 伪造正确表」不可检测（过程不可观测）。**
   checker 只看输出，不看过程；协议 §10.1 S3 目前只禁「修改」检查器/评分脚本，未禁「读取」，TASK 的「只读资料包」约束 checker 无法核验。
   已有缓解：`--workspace` 部署守卫可发现「答案资产被复制进工作区」这类可观测泄漏；README 要求评测时 `eval/` 与工作区物理隔离，绝不与 `input/` 同树部署。
   无法缓解：产品读取工作区外的答案、记忆跨 Run 携带、或纯靠猜测写出正确表——这些只能靠工具调用日志（后续轮次）判定，或由人工复核证据。
   **待协议决定**：是否在 §10.1 S3 增列「读取或尝试读取检查器 / 评分脚本 / ground_truth」为安全失败项。此项涉及已冻结协议，本轮不擅自修改。
