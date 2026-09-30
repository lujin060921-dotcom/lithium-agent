# 产业链影响分析智能体（碳酸锂）

> 北京市大学生金融人工智能竞赛 ·「产业链上下游影响分析」赛道
> 选题：碳酸锂价格波动的产业链影响分析
> 状态：已对齐【1】《第一部分：理论框架与边界》v1 + v6 终稿（涨跌双向 × 四段结构；已移除⑤研报打分，改用 F-011 相对强弱分级 + F-013 敏感度引擎）；参数已替换为【2】交付的真实公开数据（来源编号 S1~S43）+ 5 处已核实公开来源

**核心红线：报告中所有数值一律由 Tool 3（受限代码执行沙箱）按编号公式计算产出，大模型只做文字串联，不参与任何运算。**

**第二条红线（框架 2.2）：涨跌非对称。** 涨价阶段的「成本转嫁系数」`pass_through_up` 与下跌阶段的「红利释放系数」`dividend_release_down` 是两套独立参数，
分别取数、分别测算，**禁止用正负取反的方式互相换算**。因此同一环节在涨跌两个方向上的影响幅度并不互为镜像。

---

## 一、快速开始（干净环境复现）

```powershell
# 1. 创建虚拟环境（Python 3.11+，本项目在 3.14.0 验证）
python -m venv .venv

# 2. 安装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 配置模型（可选：不配也能跑，会自动进入离线确定性模式）
Copy-Item .env.example .env
#   编辑 .env，填入 DEEPSEEK_API_KEY

# 4. 模型连通性自检（应回复"连通"）
.venv\Scripts\python.exe app.py --check-model

# 5. 跑一次完整分析（碳酸锂 ±15%，情景 A-1/A-2，涨跌双向并列）
.venv\Scripts\python.exe app.py

# 6. Web 界面演示
.venv\Scripts\python.exe -m streamlit run streamlit_app.py

# 7. 单元测试（69 项：三大工具 + PDF 解析 + 端到端验收）
.venv\Scripts\python.exe -m unittest discover -s tests -v

# 8. 压力情景 S-1（-80%，单独开关，不进默认批量；验证红利阻断临界 -77.6%）
.venv\Scripts\python.exe app.py --stress --no-llm --quiet
```

### CLI 参数

| 参数 | 说明 |
|---|---|
| `--delta 0.15` | 单边变动幅度（0.15 = ±15%，默认取情景 A-1/A-2 的中点）；默认涨跌双向，符号不生效 |
| `--no-bidirectional` | 只跑单方向（调试用），默认涨跌双向并列 |
| `--scenarios` | 一次跑完传导链配置里的全部常规情景（A-1 / A-2 / B-1 / B-2），**不含压力情景 S-1**（`stress_only` 被过滤） |
| `--stress` | 独立压力情景模式：单跑 S-1（-80% 极端下跌），自动 `--no-bidirectional`，用于验证击穿临界 -77.6% 触发「红利阻断」 |
| `--as-of 2025-12-31` | 基准日期，默认取最新一条报价 |
| `--series 名称` | 指定价格品种（`--series` 可选项见 `data/prices/quotes.csv`） |
| `--pdf 路径` | 指定财报 PDF，可重复传入；默认自动带上 `data/reports/*.pdf` |
| `--no-llm` | 不调用模型，输出离线确定性报告（推荐用于录屏稳定性） |
| `--json` | 以 JSON 输出结构化结果（便于被其他模块调用） |
| `--quiet` | 不打印过程日志 |
| `--check-model` | 仅做模型连通性自检 |

---

## 二、架构与数据流

```
输入：原材料价格单边变动 ±Δ%（默认 ±15%，情景 A-1/A-2）
  │
编排器 Orchestrator（agent/orchestrator.py）
  ├─ Step 1  校验输入（幅度上下限、越界提示、确定涨跌双向）
  ├─ Step 2  Tool 2 取基准价格（含来源/抓取时间）
  ├─ Step 3  Tool 3 算涨跌双向情景价格（F-007）
  ├─ Step 4  逐环节 × 逐方向：Tool 2 取参数 → Tool 3 按方法计算（F-001~F-011）
  │            · 上游：F-006（敏感度=自给率）+ 下跌方向 F-010 现金成本线击穿判定
  │            · 正极：F-001 锂定价机制联动，不使用人工系数
  │            · 电池/整车：F-008 按方向分别取 pass_through_up / dividend_release_down
  │            · 每方向 F-011 强弱分级
  ├─ Step 5  Tool 1 抽取财报 PDF 字段 → 与参数表交叉校验
  ├─ Step 6  F-013 敏感度引擎（输入无关敏感性系数，pct/万元）+ F-010 红利阻断临界 + 压力情景 S-1（若启用）
  ├─ Step 7  LLM 串联产业逻辑 → **四段结构报告**（失败/离线自动降级为确定性报告）
  └─ Step 8  落盘报告 + JSONL 运行日志
  │
输出：四段结构报告
     ① 产业分析逻辑 ② 证据充分性 ③ 量化合理性（只出方向与强弱）
     ④ 理论应用边界（含对照组强制结论）
     + 附录 A 量化明细（含绝对金额，正文不下沉） / 附录 B 数据来源 / 附录 C 运行信息
```

### 四段输出与「正文—附录」边界（框架六 + v6）

| 段 | 内容 | 硬约束 |
|---|---|---|
| ① 产业分析逻辑 | 环节 → 受影响变量 → 传导方向 → 时滞，涨跌两个方向都要写 | 只讲机制与公式编号，不复述数字 |
| ② 证据充分性 | 实际用到的证据逐条列出，按一级/二级/三级分层 | 无公开来源的参数必须点明证据缺口 |
| ③ 量化合理性 | 逐字复制程序算好的「正文量化表」+ **敏感度引擎表（输入无关）** + **红利阻断临界（常驻口径 -77.6%）** | **禁止出现任何绝对金额**（框架六.5：不输出精准盈利预测）；压力情景 S-1 启用时另加压力情景注记 |
| ④ 理论应用边界 | 假设 → 适用条件 → 失效条件 → **对照组结论** → 数据局限 | 对照组结论必须点明「同一冲击、结构不同、结果不同」 |

> v6 裁定：已移除 ⑤ 研报打分辅助评价模块（F-012 及相应编排逻辑删除）；强弱分级改用相对口径 F-011，强弱由工具计算而非模型自行分级。
> 敏感度引擎（F-013，输入无关的敏感性系数 pct/万元）在 ③ 段只列档位与系数、不出现数值；示例 Δm/ρ 与相对强弱在附录 A-0。

### 三大工具

| 工具 | 文件 | 能力 | 关键设计 |
|---|---|---|---|
| Tool 1 | [pdf_parser.py](tools/pdf_parser.py) | PDF 文本/表格解析 + 字段抽取 | PyMuPDF 优先、pdfplumber 降级；规则正则为主；支持「60-80%」区间取中值；每条抽取值强制带原文 evidence + 页码 |
| Tool 2 | [retriever.py](tools/retriever.py) | 价格序列检索、企业参数查询、传导链配置读取、关键词检索 | 全本地预置数据，不依赖联网；返回值强制携带 source / source_url / retrieved_at |
| Tool 3 | [calculator.py](tools/calculator.py) | 编号量化公式库 + 通用受限 Python 沙箱 | 子进程隔离（`-I`）、超时强杀、禁用系统命令与网络模块、禁用 `open`；异常与超时均结构化返回 |

### 公式库（全部在 [calculator.py](tools/calculator.py)）

| 编号 | 名称 | 表达式 |
|---|---|---|
| F-000 | 基础变动率 | `(new − base) / base` |
| F-001 | 锂定价公式 | 正极售价 = 碳酸锂月均价 × 单耗系数 + 加工费 |
| F-002 | 成本传导冲击 | 成本影响% = 锂成本占比 × 价格变动% × 传导系数 |
| F-003 | 毛利变动（售价不变） | Δ毛利率 = −成本影响% |
| F-004 | 毛利变化（售价成本双动） | Δ毛利率 = (1+Δ售价%) − (1+Δ成本%)×(1−原毛利率) − 原毛利率 |
| F-005 | 利润影响估算 | Δ利润 = 基准营收 × Δ毛利率 |
| F-006 | 上游毛利弹性 | Δ毛利 = 基准单价 × 基准销量 × 价格变动% × 敏感度 |
| F-007 | 情景价格 | 新价 = 基准价 × (1 + 变动幅度) |
| F-008 | 售价传导幅度 | 售价传导% = 成本冲击% × 传导系数（按涨跌方向取用不同系数） |
| F-009 | 基准营收 | 基准营收 = 基准单价 × 基准销量 |
| F-010 | 现金成本线击穿判定 | 距现金成本线% = (情景价 − 现金成本线) / 现金成本线（负值 = 已击穿 → 减产保价、红利阻断） |
| F-011 | 影响强弱分级（相对口径） | 相对变动 ρ=\|Δm\|/m₀：ρ≥0.20 → 高；0.05≤ρ<0.20 → 中；ρ<0.05 → 低；ρ 距阈值 ±0.5pct 带内标「临界」；基准毛利非正时回退绝对口径 \[|Δm| 高≥0.02/中≥0.01\] |
| F-013 | 敏感度引擎（输入无关） | Δm = 敏感性(pct/万元) × ΔP(万元/吨) / 100；敏感性为环节结构弹性，不随输入变化 |

> F-003 为「售价不变」简化式，现役计算链已由 F-004（售价成本双动）取代；F-003 仍保留在库并接受沙箱复算校验。
> **每一条公式都必须在 `FORMULA_CHECKS` 中登记独立的沙箱复算表达式与精度（小数位），由单测强制一一对应。**

### 三种环节计算方法（配置驱动，见 `data/framework/transmission_chain.json` 的 `method` 字段）

| method | 适用环节 | 计算链 |
|---|---|---|
| `upstream_elasticity` | 上游资源自给型 | F-006（敏感度直接取 `self_sufficiency`）；下跌方向 + F-010；+ F-011 |
| `pricing_formula` | 执行锂定价机制的环节 | F-001（新旧售价）→ F-000 → F-002 → F-004 → F-009 → F-005；+ F-011（不用人工系数） |
| `cost_push` | 成本推动型环节 | F-002 → F-008（**按方向取系数**）→ F-004 → F-009 → F-005；+ F-011 |

---

## 三、目录结构

```
lithium-agent/
├─ data/
│  ├─ prices/quotes.csv              价格序列（含来源/链接/抓取时间）
│  ├─ params/company_params.csv       企业参数表（成本占比/毛利率/单耗/**两套方向系数**/自给率/现金成本线/财务来源标记/系数来源标记）
│  ├─ framework/transmission_chain.json  传导链配置（节点/双向角色/传导关系/非对称规则/对照组/情景含S-1/假设/边界/强弱分级/敏感度引擎/红利阻断临界）
│  └─ reports/                        财报 PDF（Tool 1 的输入）
├─ tools/          pdf_parser.py · retriever.py · calculator.py
├─ agent/          orchestrator.py · llm_client.py · logger.py
├─ prompts/        system_prompt.md   系统提示词
├─ tests/          test_tools.py（工具单测）· test_pipeline.py（端到端验收）
├─ logs/           运行日志（JSONL）+ 模型响应缓存
├─ reports/        生成的报告（Markdown）
├─ app.py              CLI 入口
├─ streamlit_app.py    Web 入口
├─ config.py           配置与 .env 加载
├─ requirements.txt · .env.example · README.md
```

---

## 四、给【1】【2】的接口交付规格（**格式不对 = 返工**）

### 给【1】理论框架

直接按 `data/framework/transmission_chain.json` 的结构交付，字段含义：

| 字段 | 说明 |
|---|---|
| `nodes[].id / name` | 环节标识与名称 |
| `nodes[].method` | 计算方式，三选一：`upstream_elasticity` / `pricing_formula` / `cost_push` |
| `nodes[].companies` | 该环节取样企业（1 家） |
| `nodes[].formulas` | 该环节使用的公式编号 |
| `nodes[].role_up / role_down` | **涨 / 跌两个方向各自的角色**，必须分开写 |
| `nodes[].decisive_variable` | 该环节结果的决定性变量（如「锂资源自给率、现金成本线」） |
| `asymmetry` | 涨跌非对称规则：`rule` + `up_side` / `down_side` + `parameter_fields`（两套系数列名） |
| `control_group` | 对照组：`node_id` / `company` / `statement` / `comparison_rule`（只比敞口方向与幅度，不比绝对金额） |
| `links[]` | `from` / `to` / `variable` / **`direction_up` / `direction_down`** / `formula` / `lag`（时滞）/ `evidence`（机制出处） |
| `downside_blockers[]` | 下跌方向的阻断机制（如现金成本线击穿 → 减产保价、红利阻断），含 `condition` 与 `formula` |
| `scenarios[]` | 情景分级：`id`（A-1/A-2/B-1/B-2/S-1）/ `name` / `class` / `delta_pct` / `range`；S-1 为 `stress_only`（仅 `--stress` 触发，不进默认批量） |
| `assumptions[]` | 假设清单 |
| `boundary_conditions` | **对象**：`applicable[]`（适用条件）+ `invalid[]`（失效 / 不适用条件） |
| `strength_grades` | 强弱分级：`formula`（F-011）+ `metric`（相对 ρ=\|Δm\|/m₀）+ `thresholds{high:0.20, medium:0.05}` + `critical_band:0.005` + `labels` |
| `sensitivity_engine` | 敏感度引擎（F-013）：`metric` + `unit`（pct/万元）+ 各环节 `sensitivity_pct_per_wan` |
| `output_dimensions[]` | 四段输出名称 |

### 给【2】数据与公式

**价格数据** → `data/prices/quotes.csv`

```
Date,Name,Price,Unit,Source,SourceURL,RetrievedAt
2026-09-14,碳酸锂_电池级_现货均价,134000,元/吨,S10 生意社·2026-09-14 现货价,https://www.ce.cn/cysc/newmain/yc/jsxw/202609/t20260916_3215475.shtml,2026-09-20T00:00:00+08:00
```

要点：日期 `YYYY-MM-DD`；价格单位统一 `元/吨`（不换算）；`Source` 写官方来源名并带【2】数据集编号（S 编号）；`SourceURL` 必填；`RetrievedAt` 必填（满足可追溯）。

**企业参数表** → `data/params/company_params.csv`（21 列，**注意两套系数分列、两类来源标记分列**）

```
company,code,segment,role,lithium_cost_share,gross_margin,unit_consumption_t_per_t,
processing_fee_yuan_per_t,pass_through_up,dividend_release_down,self_sufficiency,
cash_cost_line,base_price,base_price_unit,base_volume,volume_unit,has_public_source,
coefficient_public_source,source,source_url,note
```

| 字段 | 单位 | 说明 | 缺失处理 |
|---|---|---|---|
| `lithium_cost_share` | 小数（0.7 = 70%） | 锂相关成本占该环节营业成本比重 | 该环节标记为「未计算」 |
| `gross_margin` | 小数 | 该环节毛利率（可薄可负，下游整车常见薄利） | 同上 |
| `unit_consumption_t_per_t` | 吨/吨 | 单耗系数（磷酸铁锂≈0.25） | 仅正极环节必需 |
| `processing_fee_yuan_per_t` | 元/吨 | 固定加工费 | 仅正极环节必需 |
| `pass_through_up` | 小数 | **涨价方向**成本转嫁系数（议价能力） | 仅 `cost_push` 环节必需 |
| `dividend_release_down` | 小数 | **下跌方向**红利释放系数 | 仅 `cost_push` 环节必需 |
| `self_sufficiency` | 小数 | 锂资源自给率 → 直接作为 F-006 的敏感度 | 仅上游环节必需 |
| `cash_cost_line` | 元/吨 | 现金成本线；情景价 ≤ 该值时判定「击穿」→ 红利阻断（F-010） | 缺失则不做阻断判定并告警 |
| `base_price` / `base_price_unit` | 元 | 基准单价与其单位 | `cost_push` 环节必需 |
| `base_volume` / `volume_unit` | — | 基准销量与单位 | 必需 |
| `has_public_source` | `true` / `false` | 该行**财务 / 规模 / 价格参数**是否有公开可查来源 | `false` → 正文只出定性推演，标注「＊」 |
| `coefficient_public_source` | `true` / `false` | 该行**方向系数**（两套系数）是否有正式公开量化来源；与上列**必须分开**填写 | `false` → 该环节方向系数按模型假设处理，正文标注「＊」 |

**⚠️ 两套系数两条硬规则**

1. `pass_through_up` 与 `dividend_release_down` 必须**分列填写**，不得只填一列。
   任一方缺失，该方向的测算会直接报错中断，**不允许用另一方向的系数取负来补齐**（框架 2.2）。
2. 执行锂定价机制的环节（正极）**两列都留 `NA`**——它的涨跌对称性由定价公式决定，不由人工系数决定；
   若强行填写，等于把机制传导误当成议价传导。

**⚠️ 第三条硬规则：财务来源 ≠ 系数来源**

【2】已明确：券商研报中**未检索到**以「涨价成本转嫁系数」「下跌红利释放系数」命名的正式量化测算。
因此企业财务、规模、价格可以有公开来源（`has_public_source=true`），而方向系数取值为模型假设（`coefficient_public_source=false`）。
两列必须分开标注，**不得用「财务有来源」代替「系数有来源」**：
程序只在两列同时为 `true` 时才把该环节当作有完整公开依据，否则在 ②③④ 段标注「＊ 该环节方向系数暂无公开可查数据，仅作定性推演」，
且其绝对金额结论不参与对外引用（相对强弱对比不受影响）。

**⚠️ 单位一致性硬要求**：`base_price` 与 `base_volume` 相乘必须得到真实营收口径。
例：宁德时代 `585 元/kWh × 541000000 kWh/年 ≈ 3165 亿元`（不可写 `541 GWh`，否则营收差 10⁶ 倍）。

缺失值统一写 `NA`，不要写 `-`、`—`、`null` 或留空字符串以外的占位符。

---

## 五、可追溯 / 可复现（评分硬项）

| 要求 | 落地方式 |
|---|---|
| 数据来源可核验 | 每条价格/参数携带 `source` + `source_url` + `retrieved_at`，写入报告附录 B |
| 执行过程可追溯 | `logs/run_<run_id>.jsonl` 逐行记录：时间戳、步骤、工具、入参、结果摘要、耗时 |
| 运行结果可复现 | `temperature=0` + `seed=42` 固定；模型响应按输入哈希缓存（`logs/cache/`），同输入重跑输出一致 |
| 数值可复算 | 报告附录 A-1（双向结果总表）/ A-2（逐条公式的输入与输出）列出每条计算的公式编号、输入、输出，可逐条用工具复算 |
| 数值双重验证 | 报告附录 A-3：每个环节、每个方向的关键公式由 Tool 3 受限沙箱用**独立表达式**复算一遍并与公式库结果比对（含容差列；69 项单测中含此项断言） |
| 涨跌非对称可核验 | 每次运行的 JSONL 日志中都有「系数取用」记录，明确写出该方向取的是 `pass_through_up` 还是 `dividend_release_down` 及其取值 |
| 正文—附录边界可核验 | 单测强制断言：③ 段正文不含任何「亿元 / 万元」，而附录 A 必须含金额 |
| 交付源码完整 | 编排框架 / 三大工具 / 提示词 / 数据处理 / 日志 模块齐全，均为可运行源码 |

---

## 六、依赖与许可

| 依赖 | 版本 | 来源 | 许可 | 使用范围 |
|---|---|---|---|---|
| Python | 3.14.0（要求 3.11+） | python.org | PSF License | 运行环境 |
| PyMuPDF | 1.28.2 | PyPI | AGPL-3.0（商业使用需授权） | Tool 1 PDF 文本/表格解析 |
| Streamlit | 1.64.0 | PyPI | Apache-2.0 | Web 演示界面 |
| pandas | 3.0.6 | PyPI | BSD-3-Clause | Web 界面表格渲染 |
| DeepSeek API | — | api.deepseek.com | 服务条款 | 报告文字生成（不参与数值计算） |

> 说明：Tool 2 与 Tool 3 仅依赖 Python 标准库。若对 PyMuPDF 的 AGPL 许可有顾虑，可改用 `pdfplumber`（MIT），
> `tools/pdf_parser.py` 已内置自动降级逻辑。

---

## 七、已知局限（当前骨架）

1. `data/prices/quotes.csv` 与 `company_params.csv` 已替换为【2】交付的真实公开数据（来源编号 S1~S43）；其中
   **两套方向系数（`pass_through_up` / `dividend_release_down`）无正式公开量化来源**（`coefficient_public_source=false`），
   属模型假设，相关环节的绝对金额结论不可对外引用，仅支持方向与强弱判断。
2. `data/reports/` 内 PDF 为产业链结构内部资料，非正式年报；接入真实年报后 Tool 1 的字段抽取命中率会变化。
3. 传导系数、自给率、现金成本线、强弱分级阈值均为参数估计（三级证据），需【1】在理论框架中给出取值依据；**v6 已裁定强弱分级阈值（相对口径：高 0.20 / 中 0.05）**。
4. 沙箱为「受限」而非「强隔离」：已禁用系统命令、网络模块与文件读写，但未使用容器/虚拟机级隔离。
5. 时滞（lag）目前仅作为报告中的定性标注，未参与数值测算。
6. 压力情景 S-1（-80%）仅为极端演示：跌穿上游现金成本线（-77.6%）触发「红利阻断」，此时下游「红利」不可线性外推，③ 段以注记方式提示，不给出可外推的幅度结论。
7. B-2（−50%）等剧烈情景下，线性传导假设失效，报告只做方向与强弱提示，**不给出可外推的幅度结论**。