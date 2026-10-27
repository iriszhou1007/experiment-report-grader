# 实验报告批改 Skill 使用说明

这个 Skill 用于批量读取 PDF 实验报告，按照教师确认的评分标准生成分项分数、总分和综合评语。默认从 100 分起评，只对“核心任务缺失、明确问题未回答、纯图片无分析、纯文字无所需图片、关键判断与证据直接矛盾”等清楚且可复核的事实扣分；通用默认规则不因篇幅短、解释不够深入或个人写作风格扣分；实验一则按教师补充要求检查截图编号、正文引用/描述及条件性首行缩进，详见专用细则。没有确认问题时允许满分。模型输出扣分事实，程序计算分数。盲复评超差报告可进入审核页，但未经教师显式裁决不得批准或写回。

## 0. 在一台新机器上安装（zip 方式）

本 Skill 以 **zip 包**发布，目标机器不需要 git、不需要联网（除了装 Python 依赖那一步）。解压后按顺序做四件事：

**① 确认 Python。** 需要 Python 3.10 以上、**64 位**。在命令行里试 `python --version`，不行就试 `python3 --version` 或 `py -3 --version`，哪个能回答就用哪个。装在 Microsoft Store 里的 Python 常出问题，建议用 python.org 的安装包或 conda。Windows 上再执行一次 `chcp 65001`（或设 `PYTHONUTF8=1`），否则中文输出会乱码。

**② 自检并安装依赖。** 在解压出来的目录里执行：

```bat
python scripts\bootstrap.py            :: 只报告：缺什么、每机器资产从哪找到的
python scripts\bootstrap.py --install  :: 安装缺失的 Python 依赖
```
（macOS / Linux 把 `python` 换成 `python3`，路径分隔符换成 `/`。）

**③ 配置教师签名（必做）。** 可提供带 Ink 笔迹标注的 PDF，或背景透明、紧密裁切的 PNG。签名是个人凭证，不随包分发，也不会自动绘制、抠图或改造；若在对话中附图，先确认该图片是获授权使用的教师签名。未配置或文件不可用时，`bootstrap` 会标 `BLOCK`，评分前的 `prepare` 会停止。任选一种方式配置：

```bat
setx EXPERIMENT_REPORT_GRADER_SIGNATURE_FILE "C:\Users\你\Documents\签名.png"
```
也可使用 `--signature-file` / 兼容旧参数 `--signature-pdf`，写入 `config\local.json`（可先用 `python scripts\bootstrap.py --write-config` 生成），或把 PDF/透明 PNG 放到 `assets\signature\`。旧环境变量 `EXPERIMENT_REPORT_GRADER_SIGNATURE_PDF` 继续兼容。PNG 原样用于教师批准后的签名栏写回；若图片带不透明背景、过小或格式损坏，前置检查会停止。

**④ 注册到你的 AI 工具。** 先看它要装到哪，再真正执行：

```bat
python scripts\install_skill.py --host workbuddy --dry-run
python scripts\install_skill.py --host workbuddy
```
`workbuddy` 可换成 `codebuddy` / `claude` / `codex` / `dsh`；`--host auto` 会自动探测本机装了哪些。注册目标默认是各宿主的用户级技能目录（如 `C:\Users\你\.workbuddy\skills\`）。Windows 上没有开发者模式时脚本会自动改用目录联接；都不行时加 `--copy`（代价是技能更新后要重新拷贝，且副本里没有你的 `config\local.json`）。

**装完自检：**

```bat
python scripts\batch_pipeline.py doctor
python scripts\batch_pipeline.py doctor --self-test
```

`doctor` 会逐项列出每个外部路径"是从哪一层找到的"；`--self-test` 用合成数据验证产物写出器——把嵌进审核面板的评语与批次提示读回来比对，并确认分数偏移真的改变了表格。想先拿 5 份报告跑一遍全流程（prepare → 填决策 → finalize → 写回干跑）再正式开工，见 `SKILL.md` 的「First run on a new machine」。

**Windows 不只查 C 盘。** 自检先查指定路径、环境变量、配置文件和 `PATH`；仍未定位可用程序时，会在每个本地固定磁盘的常见安装目录中有界搜索 Tesseract 与 Poppler（含版本号目录、空格和中文目录）。不遍历整个磁盘、不搜索网络盘/移动盘，不跟随目录链接；搜索深度、目录项数和时间均有预算，结果会注明范围及是否触及预算。找不到只表示“该范围内未定位到可用程序”，不是“电脑未安装”；先提供其他盘已有程序的完整路径，再考虑安装。找到后检查版本启动及 Tesseract 的 `chi_sim`、`eng` 语言数据。启用 OCR 时两项任一不可用，`prepare` 都会在创建批次前停止。导入/注册 skill 本身不会自动执行这些检查。

**发布方**（把包做出来的人）执行：`python3 scripts/package_skill.py --out dist/ --verify`，会生成 zip、tar.gz 与 `SHA256SUMS`，并自动解包复验（排除学生报告、本机配置、OCR 生成物与签名模板）。

## 1. 准备实验报告

建议每次实验单独建立一个文件夹，并把本次需要批改的 PDF 直接放在里面：

```text
实验报告
├─ 实验1
│  ├─ 01_实验1_学号_姓名.pdf
│  └─ ...
├─ 实验2
└─ 实验3
```

文件名最好包含实验名称、学号和姓名。不要提前创建批改结果文件夹，Skill 会自动创建。

## 2. 开始使用

在 Codex 中可以一次说清楚并直接生成草稿：

```text
使用 experiment-report-grader Skill。
报告文件夹：D:\课程\实验报告\实验1
实验主题：实验1：Web服务器搭建与HTTP协议分析
使用默认评分标准和整数分。检查完目录后，先问我选择哪种报告检查模式，再生成评分草稿、审核页面和 Excel，不要写入 PDF。
```

开始时只需要提供实验报告文件夹地址。Skill 会先检查目录里的文件类型、文件名和可能的实验主题，再检查匹配的评分标准、评分设置及运行前置条件；之后汇报检查结果。评分前，Skill 会在对话中请你选择报告检查模式；这一步发生在生成评分草稿和打开审核面板之前。审核面板只用于复核已生成的评分草稿。面板会列出需处理的预检状态；文件名缺少姓名或学号时标记为需重交，不进入评分和成绩统计；文件名显示多个实验主题时会暂停评分，先请教师拆分或确认。只提供 `.doc/.docx` 的报告会标记为“需重新提交 PDF”，不参与评分和批次统计；补交 PDF 后点击“加载 / 创建批次”重新扫描，再在当前 Codex 对话中一次性请求批量评分即可，Skill 会把已确认补交和其他尚无评分记录的合格 PDF 一并处理。OCR、签名等问题不会在一开始询问。

Skill 默认采用整数分。模型不再凭整体印象选分数档，而是逐项记录扣分事实、页码与确定性，程序按已确认规则计算分数并禁止同一事实重复扣分。默认规则不设置最低扣分：找不到明确问题时使用空扣分清单并获得满分，不得为了控制分布而虚构一般性不足。随后从同一证据包完成第二次盲评；超过 2 分的报告仍列为“待审核”，并标注“双评有差异”；教师核对两次评分并确认当前分数后才能批准。默认上报成绩的全批平均分不低于 90；需要调整时系统使用最小统一加分，原始批改分仍保留可追溯。

“实验1：Web服务器搭建与HTTP协议分析”以[新版正式指导书](assets/guides/experiment-1-web-http.pdf)、[教师版评分细则](references/experiment-1-web-http.md)和 `assets/rubrics/experiment-1-web-http.json`（v6）为准。IIS / Apache二选一，分值30/35/25/10；RPT-1至4来自新版第7节，RPT-5是教师明确补充的编排要求，PDF原件未改。每张实验截图必须有图号、正文按图号引用和描述；每张问题截图扣2分，2张扣4分、3张及以上封顶6分；同一张图的多个缺陷不重复扣。仅当整份报告无其他扣分时，原页确认普通中文正文首行未缩进才扣3分；已有任何其他扣分就不启用缩进项，程序禁止跨项叠加。其他排版、审美和前文操作提示不新增扣分。内容与格式全合格仍允许100分，不硬封顶；92-98超过80%仍只是观察目标。旧试评保留为历史结果，重评须重新prepare使用v6，不能静默加减旧成绩；其他实验不自动套用本补充要求。

“实验2：DNS服务器配置与DNS协议分析”使用[正式指导书](assets/guides/experiment-2-dns.html)、[教师版评分细则](references/experiment-2-dns.md)和 `assets/rubrics/experiment-2-dns.json`（正式发布v1.1）。分项为Packet Tracer配置与验证30分、真实DNS抓包分析35分、nslookup查询复核25分、环境与报告编排10分。十道分析题按本人命令、报文、帧号及实际DNS环境核对；不要求固定公网IP、CNAME链、响应记录数或ping结果。已明确的Packet Tracer CNAME响应中ANCOUNT与可见Answer条数不一致不扣分。RPT-5沿用教师补充的截图编排和条件性首行缩进规则。`.pkt`工程和`.pcapng`附件提交情况与PDF评分分开核验；仅凭报告PDF或当前文件夹不能推断未提交附件，也不据此扣PDF分。实验二重新评分须明确选择这份评分标准，不能套用实验一的题目要求。

仅文字模式仍会把 OCR 识别结果并入证据，但 OCR 不保留可靠的页面布局。默认评分表的 `hard_to_read` 档因此设有视觉扣分门槛：只有实际打开相关 PDF 页面并记录视觉核验页后才能选择。这个检查按扣分触发，不会要求逐份视觉复核整批报告。

## 3. 评分前选择报告检查模式

目录和评分标准确认后、运行 `prepare` 之前，在对话中选择“仅文字 + OCR”“智能检查”或“完整视觉检查”。审核面板不提供此选择，因为它是在评分草稿生成后打开的。

### 强制核验最新稳定版本

每批 `prepare` 都会在线查询 GitHub 上版本号最高的正式稳定 Release，并比对本机 Skill 版本、Git commit/文件树和当前评分标准文件哈希。未发布 tag、预发布版、`main` 和本地缓存都不能替代稳定版本。无法联网、仓库没有稳定 Release、代码或评分标准与稳定版本不一致时，流程会在创建批次文件前停止；不会自动覆盖或更新本机 Skill。先由维护者发布获批的 `vMAJOR.MINOR.PATCH` 稳定版本，再把 Skill 更新到该版本。可手动查看：

```powershell
python scripts/check_stable_version.py --rubric assets/rubrics/experiment-1-web-http.json --json
```

`prepare` 会再次强制核验，并把成功结果记录在暂存清单和 `批次信息.json`。安装包没有 Git 历史时，核验器会逐文件比对安装目录与 GitHub 稳定 tag；评分标准必须随稳定版本发布在 Skill 仓库内。详细规则见 [`references/stable-version-check.md`](references/stable-version-check.md)。

## 4. 在审核页面批改

需要打开页面时，对 Codex 说：

```text
启动审核服务并打开审核页面。
```

建议将 Codex 提供的 `http://127.0.0.1:8765/` 审核网址复制到 Edge、Chrome 等浏览器中打开，这样查看原始 PDF、修改分数和连续审核会更方便。不要直接双击生成的 HTML 文件进行正式审核：这种 `file://` 离线页面无法直接写入本地 JSON、Excel 或 PDF，只适合查看和存档。

页面中的主要操作：

1. 在“实验报告文件夹”中输入路径，或从下拉列表选择实验文件夹。
2. 从左侧报告列表选择学生，右侧可查看对应的源 PDF。
3. 检查总分和综合评语；需要时修改分项分数或评语。
4. 点击“保存分数和评语”，保存草稿并更新 Excel，不修改 PDF。
5. 设置是否在原页添加手写评语、逐页画勾、加盖教师签名、手写成绩，并选择反馈页插入首页或追加末页。签名模板仍是批改前必须满足的固定条件；此处的签名开关只决定本份 PDF 是否实际加签。
6. 点击“批准并直接写回 PDF”。确认对话框会显示学生、源 PDF 和写回选项；确认后系统先备份原件，再直接写回源 PDF，不再经过单独的预览页。

左侧“待审核”筛选包含常规待审核和双评有差异的报告；有差异的报告额外标注“双评有差异”。其评分须先核对两次结果并确认最终分数，才可批准写回。

审核中发现报告需要补交时，可点击“标记需重新提交”。旧评分会从成绩表和平均分中排除，面板会保留退回前的 PDF 与评分记录。学生补交 PDF 后，替换原文件并确认收到；面板显示“已重新提交”，可复制重新评分指令到当前 Codex 对话启动新一轮评分。Word 文件的预检状态也会在同一批次目录出现匹配 PDF 后显示为“已重新提交”。

连续审批时，批准当前报告后页面会自动切换到下一份未审核报告；“连续审批 · 不再提示”开关仅用于跳过之后逐份出现的确认对话框。

## 5. 查看结果

每个实验批次会在报告目录中生成两个并列文件夹：

```text
实验报告目录
├─ 学生报告.pdf
├─ 实验1_评分暂存
└─ 实验1_批改结果
```

`实验1_评分暂存` 保存评分输入、盲复核、OCR 缓存和临时写回文件。新批次始终把这些过程文件放在用户提供的报告目录下，不再堆在 Skill 工作区。

`实验1_批改结果` 保存正式成绩和审核记录：

```text
实验1_批改结果
├─ 实验报告成绩汇总.xlsx
├─ 实验报告审核面板.html
├─ 复评分差异审计.json
├─ 批次信息.json
├─ index.md
├─ reviewjson
└─ 原始数据
```

### 清理评分暂存文件

按钮会在以下条件全部满足后启用：

- 评分与盲复核均已完成；
- 所有报告均已教师审批，没有待补交报告或未确认的双评差异；
- 成绩表、审核面板、批次信息和本批次评分清单均存在且有效。

点击后，面板会先显示暂存目录的完整路径、文件数和占用空间，再要求确认。确认后只清理当前批次的 `<报告目录>/<批次名>_评分暂存` 文件夹。

如果按钮仍不可用，按钮旁会显示未满足的条件，例如“暂存目录不存在”“缺少本批次评分清单”或“还有报告未审批”。

清理只删除评分暂存目录，不会删除：

- 学生 PDF 和批改结果文件夹；
- 成绩表、`reviewjson`、审核面板和差异审计；
- `原始数据` 中写回前的 PDF 备份。

旧批次位于报告目录下的 `tmp/experiment-report-grader/<批次名>` 时，面板仍可识别该路径。Skill 不会自动清理其他工作区或来源不明的目录。命令行 `prepare` 也会拒绝把新批次暂存目录指定到报告目录以外的位置。

## 6. 常用指令

```text
继续批改上次未完成的实验报告。
```

```text
重新打开当前实验批次的审核页面。
```

```text
只重新批改尚未审核的报告，不要覆盖已批准的结果。
```

```text
停止审核服务并清理临时文件。
```

## 注意事项

- 生成评分草稿不会修改学生 PDF。
- 只有教师明确批准后，Skill 才会写回源 PDF。
- 再次批准同一份报告时，会从保存的原始备份重新生成，避免重复增加反馈页。
- 修改报告文件、评分标准或检查模式后，建议让 Codex 重新准备对应批次。
- 不要把其他实验的 PDF 混在同一个实验文件夹中。
- **每一页都会被送给模型**：文字预算只决定每页摘录多长，不决定某一页是否出现。摘录被截短时，证据包里的 `coverage:` 行会写明是哪几页被截短。
- **证据包自带评分表**（维度名、满分、分数顺序），模型不需要靠记忆去猜"第几个数字对应哪一项"。
- **建议让模型为每个分项填写页码证据**：这些页码会显示在审核页面的"页码证据"栏中，您可以据此翻到对应页核对，不必重读整份报告。
- **当前版本把漂移变成可裁决事实**：系统比较两遍的扣分 `fact`，超差报告仍可生成审核页和 Excel，但保持“待审核”并标注“双评有差异”；未在审核页显式确认前，不能批准、批量写回或变成正式 PDF 成绩。默认 rubric 的批次平均分下限为 90，系统使用可追溯的统一 `score_offset` 达成，不改写原始扣分理由。
- 若报告正文主要靠截图，OCR 会把截图识别成文字并入证据。识别结果可能有错字，证据包中已标注、模型不会据此扣分，但关键判断仍建议打开原页确认。
- 在 macOS / Linux 上启用 OCR：运行 `python3 scripts/bootstrap.py --ocr`（自动寻找已装好 OCR 依赖的 Python，生成本地辅助程序，**不需要激活 conda 环境、不需要联网**）。macOS 还可加 `--with-vision`，额外构建系统自带的 Vision 引擎。判断是否生效：批次目录下 `visual-review-plan.json` 里的 `ocr_status` 应为 `completed`。详见 `references/local-ocr-setup.md`。
- **先看本机有什么 OCR，而不是只看缺什么**：`python3 scripts/bootstrap.py` 会打印「local OCR (this machine)」一段，逐条列出默认引擎、平台自带引擎，以及在本机各环境下发现的 rapidocr / Vision / tesseract 是否已接线；`doctor` 也有一行 `ocr engine`。每台机器装法不同（macOS 自带 Vision、Windows 装 Tesseract、conda 环境里可能是 rapidocr），这一段就是给 agent 用来告诉你"你这台机器会用哪个"的。
- **Windows 上 OCR 前置检查不通过会直接停下**：Windows 没有 shim 生成器（辅助脚本是 bash），只走真实的 Tesseract。启用 OCR 时，`prepare` 要求 Tesseract（含 `chi_sim`、`eng`）与 `pdftoppm` 均可运行；否则在创建批次之前停止。先检查其他盘的已有安装或提供路径，不直接推断未安装。只有明确不使用 OCR 时才用 `--ocr-mode off` 绕过。
- **OCR 要花时间，批次开始前请留出空档**：约 **2.5–3 秒/候选页**（本地 CPU，不消耗 API 费用）。实测 4 份报告 27 个候选页约 **80 秒**；70 份截图型报告首次准备可能 **20–30 分钟**。运行期间每份报告会打印一行 `[ocr] …` 进度，不是卡死。重复运行有缓存（约 0.5 秒），但执行 `clean` 会连同缓存一起删除。
- 想换 OCR 引擎：`--ocr-engine rapidocr`（**默认**，中文截图召回更好）或 `--ocr-engine vision`（macOS 原生，快约 3 倍但密集界面截图错误较多）。默认的 `auto` 走的就是 rapidocr 那条 shim；Vision 需要显式指定，`--with-vision` 只是把它构建好待用。
- **换机器或首次使用前，先跑一次自检**：`python3 scripts/batch_pipeline.py doctor`。它会逐项报出各阶段需要的组件（签名模板验证用的 `PyMuPDF`、写回 PDF 用的 `reportlab`、Excel 导出用的 `openpyxl`、签名模板、手写字体、Node、两个 OCR 程序与 `ocr engine` 等）是否就绪，**并注明每个路径是从哪一层找到的**（命令行参数 / 环境变量 / `config/local.json` / 技能自带资源 / 系统常见位置）。想顺带验证产物写出器，运行 `doctor --self-test`：它用合成评分数据在系统临时目录写出审核面板和 Excel，把嵌进面板的评语与批次提示读回来比对，并确认分数偏移确实改变了表格，不碰任何 PDF。`PyMuPDF` 和有效签名模板缺失会阻止 `prepare`；`reportlab` 缺失会阻止最终 PDF 写回。
- **Excel 与审核面板不再依赖 Codex 运行时**：两者都由技能自带的 Python 写出器生成（Excel 用 `openpyxl`，面板是模板替换），已经没有 Node 写出器，也不再需要 `@oai/artifact-tool`。因此换到任何机器或宿主，成绩汇总表都照常生成。Node 仅用于启动交互审核服务；教师在面板逐份审核并点击“批准并直接写回 PDF”后，系统先备份原件再写回。

- **评审面板复用模板**：面板的唯一静态来源是 `assets/review-panel-template.html`，由 `scripts/build_review_html.py` 注入批次数据生成。当前布局、状态筛选、补交流程、写回选项、PDF 展示、按钮错误提示及批次结束清理见 [`references/review-panel-template.md`](references/review-panel-template.md)。后续调整请改资产模板并重建面板，不要直接改批次输出的 HTML。
- **教师签名属于"每台机器自己配"的项目，不在包里**：首次使用用 `python3 scripts/bootstrap.py` 查看状态。可配置带 Ink 的 PDF，或透明 PNG；支持 `--signature-file`、兼容参数 `--signature-pdf`、`EXPERIMENT_REPORT_GRADER_SIGNATURE_FILE`（旧 `_SIGNATURE_PDF` 也兼容）、`config/local.json` 中的 `signature_file`，或放入 `assets/signature/`。`prepare` 会验证文件；图片只接受带透明通道的 PNG，写回时维持原图、不抠图不重绘，并在批准写回前检查签名区域遮挡。
- **换机器要改的路径不必改代码**：签名模板、手写字体、解释器、Node、OCR 辅助程序都可按 `--参数` → `EXPERIMENT_REPORT_GRADER_*` 环境变量 → 技能目录下 `config/local.json`（从 `config/defaults.example.json` 复制）的顺序指定。迁移与自查清单见 `references/portability.md`，装包流程见本页开头「0. 在一台新机器上安装」。
- **写回样式由教师逐份选择**：审核面板可选择原页手写分项评语、逐页画勾、教师签名、成绩栏手写分数，以及反馈页放首页或末页。点击“批准并直接写回 PDF”后，系统先备份原件再写回；签名文件仍须在批改前通过固定前置检查，签名开关只决定本次是否加签。可用 `--signature-file`（兼容 `--signature-pdf`）和 `--score-font` 覆盖配置。
