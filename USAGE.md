# 产业链影响分析智能体 · 使用说明

> 北京市大学生金融人工智能竞赛 ·「产业链上下游影响分析」赛道
> 选题：碳酸锂价格波动的产业链影响分析
> 适配版本：v6（四段结构报告，含敏感度引擎 / 红利阻断压力情景）

本文档同时面向**队友（收件人）**与**本人（自用）**，涵盖：能做什么、怎么快速跑通、常用命令、产物在哪、必须遵守的红线。

---

## 一、这是什么

一个跑在本地 Python 的智能体：输入碳酸锂价格单边变动幅度，自动完成取数、按编号公式计算、生成**四段结构报告**（产业分析逻辑 / 证据充分性 / 量化合理性 / 理论应用边界）。

- 涨跌双向并列：涨价用 `pass_through_up`、下跌用 `dividend_release_down` 两套独立系数（框架 2.2 涨跌非对称）。
- 数值全部由 Tool 3（受限沙箱）按公式产出，大模型只做文字串联，不参与运算。
- 支持两种模式：**离线确定性**（`--no-llm`，不联网，稳定可复现，适合录屏/演示）与 **LLM** 模式（需配 API Key，输出更丰满）。

---

## 二、快速开始（给收件人 / 队友）

在项目根目录 PowerShell 依次执行：

```powershell
# 1. 创建虚拟环境（Python 3.11+，本项目在 3.14 验证）
python -m venv .venv

# 2. 安装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 自检（单测全绿 = 环境就绪）
.venv\Scripts\python.exe -m unittest discover -s tests -v

# 4.（可选）配模型 Key：复制模板并填入你的 DEEPSEEK_API_KEY
Copy-Item .env.example .env
#    编辑 .env，只填 DEEPSEEK_API_KEY 即可；不填也能跑离线模式

# 5. 能用了：跑一次标准分析
.venv\Scripts\python.exe app.py
```

> 不配 Key 也能完整跑通全流程（自动进入离线确定性模式）。

---

## 三、环境准备（本人 / 已在本机）

```powershell
# 若想用模型输出更高质量报告，先填 Key（可选）
Copy-Item .env.example .env   # 编辑 .env 填入 DEEPSEEK_API_KEY
```

`.env` 关键项（已预置，勿乱改）：

| 变量 | 值 | 说明 |
|---|---|---|
| `LLM_TEMPERATURE` / `LLM_SEED` | `0` / `42` | 固定 → 同输入重跑输出一致（可复现） |
| `LLM_MAX_TOKENS` | `8000` | 防止报告截断（默认 2400 会截断，勿调小） |
| `AGENT_OFFLINE` | 注释 | 设 `true` 强制离线 |

---

## 四、常用命令（本人自用）

**1. 跑一次标准分析**（默认 ±15%，情景 A-1/A-2，涨跌双向）

```powershell
.venv\Scripts\python.exe app.py            # 调模型
.venv\Scripts\python.exe app.py --no-llm   # 离线确定性（录屏/稳定）
```

**2. 改变动幅度**（例：只跌 10%）

```powershell
.venv\Scripts\python.exe app.py --delta 0.10
```

**3. 一次跑全部常规情景**（A-1/A-2/B-1/B-2）

```powershell
.venv\Scripts\python.exe app.py --scenarios --no-llm
```

**4. 压力情景 S-1（-80% 极端下跌，验证红利阻断）**——决赛演示卖点

```powershell
.venv\Scripts\python.exe app.py --stress --no-llm
```

**5. 指定基准日期 / 价格品种**

```powershell
.venv\Scripts\python.exe app.py --as-of 2025-12-31 --series 碳酸锂_电池级_现货均价
```

**6. 输出结构化结果**（给脚本 / 二次处理用）

```powershell
.venv\Scripts\python.exe app.py --json --no-llm
```

**7. Web 可视化界面**（决赛录屏：展示表 / 审计 / 证据 / 日志）

```powershell
.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

**8. 自检**

```powershell
.venv\Scripts\python.exe app.py --check-model
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

---

## 五、我最常跑的一套流程

```powershell
# 改过参数/配置后：先用离线标准跑一遍验证
.venv\Scripts\python.exe app.py --no-llm --quiet

# 再跑一次压力演示确认红利阻断分支
.venv\Scripts\python.exe app.py --stress --no-llm --quiet

# 有网络、想要更高质量报告时：切 LLM 模式
.venv\Scripts\python.exe app.py
```

> 改动 `data/params/company_params.csv` 或传导链配置后，先重跑单测，确认没破坏下面红线约束。

---

## 六、产物在哪

| 产物 | 位置 | 说明 |
|---|---|---|
| 报告 | `reports/报告_<时间戳>.md` | 每次运行只保留最新一份 |
| 运行日志 | `logs/run_<时间戳>.jsonl` | JSON 逐行，评审可追溯 |
| 模型缓存 | `logs/cache/` | 同输入重跑不重复调模型，保证可复现 |

报告内含：四段正文 + 附录 A-0~A-3（量化明细 / 逐条公式审计 / 沙箱复算）+ 附录 B（来源）/ C（运行信息）。

---

## 七、红线（改造后仍必须遵守）

1. **涨跌非对称**：`pass_through_up` 与 `dividend_release_down` 分列独立取数，禁用另一方向取负补齐；任一缺失方向测算直接报错中断。
2. **③ 段零金额**：正文第三段只给方向与强弱，所有金额下沉附录 A（框架六.5：不输出精准盈利预测）。
3. **比亚迪对照组**：④ 段必须点明「同一锂价冲击下，企业结构不同，结果完全不同」。
4. **数值皆由公式产出**：编号公式 F-000/001/002/004/005/006/007/008/009/010/011/013，沙箱独立复算交叉校验。
5. **单测不写报告文件**：测试写临时目录，不污染 `reports/`、`logs/`。

---

## 八、常见问题

| 现象 | 处理 |
|---|---|
| 报告被截断 / 尾部缺失 | `.env` 里 `LLM_MAX_TOKENS` 保持 `8000`，勿调小 |
| 没配 Key 也能跑吗 | 能，自动进离线确定性模式 |
| 想在 Web 里看评分/强弱口径 | 主界面已展示强弱分级口径（F-011 相对口径：高 ρ≥0.20 / 中 0.05≤ρ<0.20 / 低 ρ<0.05） |
| 改了参数想快速验证 | 跑 `app.py --no-llm --quiet` + 全量单测 |
| 推送后队友要跑 | 按本文档「二、快速开始」即可 |