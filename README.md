# scan-pdf-split

AI Agent 技能（SKILL.md 格式）：把**无文本层的扫描 PDF 按文档类别**（文件类型 × 标段/项目）拆分为多个独立 PDF。面向评标文档（打分明细、评审表、签到表、承诺书、监督报告等），也适用于任何混装扫描件。

## 功能

- **自动识别页码→类别映射**：技能自带本地批量 OCR 脚本（固定宽窗快速识别 + 空白页整页补），纯本地进程，不占模型 token
- **复杂场景**：横竖版混排、多类别交叉排列、无标题续页（继承+逐页核实）、重复扫描副本去重（`_副本` 机制，页数守恒校验不受影响）
- **拆分必校验**：每个输出文件抽查首页+末页与文件名比对，输出页数 + 副本页数 = 源文件页数
- **识别路径分级**：自带脚本（默认，开箱即用）→ OCR MCP（可选增强）→ PaddleOCR-VL（可选）→ 模型视觉读图（兜底）
- **memory.md 记忆机制**：版式参数、坑点逐次沉淀复用

## 目录结构

```
scan-pdf-split/
├── SKILL.md                    # 技能说明（agent 读取后按此执行）
├── memory.md                   # 经验库（版式参数/坑点，逐次追加）
└── scripts/
    └── pdf_split_tools.py      # 辅助脚本：probe / ocr / render / split / verify
```

## 新用户配置（一行命令）

前置要求只有一条：**Python 3.9+ 在 PATH 里**。不需要安装任何 OCR 工具，不需要配置 MCP。

Windows（Git Bash 或 PowerShell）：

```bash
git clone https://github.com/ZacharyJiao/scan-pdf-split.git && cd scan-pdf-split && python -m venv .venv && .venv/Scripts/python -m pip install pymupdf rapidocr_onnxruntime
```

macOS / Linux（仅最后一步的解释器路径不同）：

```bash
git clone https://github.com/ZacharyJiao/scan-pdf-split.git && cd scan-pdf-split && python3 -m venv .venv && .venv/bin/python -m pip install pymupdf rapidocr_onnxruntime
```

验证配置成功（任意找个 PDF）：

```bash
.venv/Scripts/python scripts/pdf_split_tools.py probe <某个.pdf>   # Windows
.venv/bin/python scripts/pdf_split_tools.py probe <某个.pdf>       # macOS/Linux
```

能逐页输出页数/尺寸/文本层信息即就绪。

## 给 agent 的自动配置说明

如果你是对话中的 AI agent：把本仓库放在 agent 的技能加载路径下（各工具约定不同，通常为用户目录下的 `skills/` 文件夹或项目内 `skills/` 目录）。开始任务前检查 `<技能目录>/.venv/Scripts/python.exe`（Windows）或 `.venv/bin/python`（Unix）是否存在；**不存在就直接执行上面的一行命令**（在技能目录内创建 venv 并安装 `pymupdf` + `rapidocr_onnxruntime`），无需向用户索要任何其他工具或配置，然后按 `SKILL.md` 的流程执行即可。

## 使用方式

- **agent 场景（推荐）**：直接对 agent 说"按文档种类拆分 xx.pdf"，技能自动完成 探查 → 样张定标 → 批量识别 → 映射审查 → 拆分 → 校验 全流程；识别不确定的页会逐条列给你确认，绝不带猜测拆分。
- **手动命令行**（脚本也可脱离 agent 单独用）：

```bash
PY=".venv/Scripts/python"            # macOS/Linux 用 .venv/bin/python
T="scripts/pdf_split_tools.py"

$PY $T probe 合并.pdf                                  # 探查页数/方向/文本层
$PY $T ocr 合并.pdf out.jsonl --keywords 签到,明细,汇总  # 批量 OCR，产出每页文本+锚点
$PY $T render 合并.pdf 拼图前缀 --pages 1,20,42          # 渲染拼图（供人工/视觉核对）
$PY $T split 合并.pdf 输出目录 plan.json                 # 按映射拆分（支持 "_副本" 去重）
$PY $T verify 输出目录/某文件.pdf 1,-1 check.png          # 渲染首页+末页供校验
```

`plan.json` 格式：`{"输出名.pdf": [页码...], "_副本": [重复扫描页码...]}`，页码支持非连续。

## 关于 OCR MCP（可选，非必需）

SKILL.md 中提到的 OCR MCP（`mcp__ocr__ocr_image`、`mcp__ocr__parse_document`）是**可选增强**，仅用于零星补漏和疑难页精修，不是依赖。未配置任何 MCP 时技能功能完整：批量识别走自带脚本，兜底走模型视觉读图。SKILL.md 的对应路径已注明"未配置则跳过"。

## 许可

MIT License，详见 `LICENSE`。
