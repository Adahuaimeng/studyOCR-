# 外部 AI Agent 调用说明

能在本机运行命令并访问图片的 Agent，可以用和桌面软件相同的 OCR 引擎。处理图片时无需打开桌面窗口，也无需填写软件内的联网 AI 设置。模型与依赖准备完成后，识别和导出可离线运行。

## 识别一次并保存结果

在软件目录打开 PowerShell，将示例里的图片名换成你的图片路径。免安装版使用 `ocr-cli.exe`。从源码运行时，将下方 `$ocr` 改成 `".\ocr-cli.bat"`。

以下命令会将结果同时写入终端和 UTF-8 JSON，并生成可编辑工作稿。

```powershell
$ocr = ".\ocr-cli.exe"
& $ocr recognize ".\印刷体.jpg" --project ".\识别工作稿.ocrproj" --json ".\识别结果.json"
```

- `recognize` 可以按顺序传入多张图片。用 `--mode mixed` 识别混排，`general` 通用识别，`handwriting` 手写增强。默认混排。只有混排模式会尝试区分同页的印刷体和手写体。
- 单图可加 `--region LEFT TOP RIGHT BOTTOM` 只识别指定区域。坐标按照片方向转正后的像素填写，原点在左上角，右、下边界不包含在选区内。
- 在支持方向与质量检查的新版中，加 `--rotation auto` 可自动判断内容方向。旧调用默认 `keep`，保持 EXIF 处理后的方向。也可用 `90`、`180` 或 `270` 明确指定顺时针角度。识别前指定选区时，坐标仍以 EXIF 处理后、额外旋转前的图片为准。
- 只需要读文字或坐标时可省略 `--project`。需要继续修改、保留图表或导出时请保存工作稿。工作稿包含原图，即使本次只识别了选区。
- Agent 已有桌面软件保存的工作稿时，可以直接读取识别结果。

```powershell
& $ocr inspect ".\识别工作稿.ocrproj" --json ".\已有结果.json"
```

## 读取 JSON

根对象的 `schema_version` 当前为 1。`pages` 按输入顺序排列，每页含 `page_id`、文件名 `name`、宽高和 `scan_area`。工作稿中未经识别的页，`scan_area` 为 `null`。

- `lines` 按软件的阅读顺序排列，包含 `line_id`、`text`、`kind`、`confidence` 和 `bounds`。
- `kind` 可为 `printed`（印刷体）、`handwritten`（手写体）、`review`（待确认）或 `omit`（不导出）。`confidence` 是原识别值，校对后仍保留供参考。
- `bounds`、`scan_area` 和 `figure_regions` 使用 `[左, 上, 右, 下]`，均为转正后原图的像素坐标。选区内的文字坐标已经换算回原图坐标。
- `figure_regions` 提供导出时可用的图表区域。`objects` 提供工作稿里已经确认的原生表格和公式。附加 `--no-figures` 可以只跳过自动图表检测。
- `layout.gutter_x` 是两栏间空隙在原图中的横坐标。单栏页为 `null`。`layout.bands` 按从上到下的顺序列出各区段的栏数。`lines` 和 `blocks` 均按阅读顺序排列。块的 `band` 和 `column` 从 0 开始，跨栏块或单栏页的 `column` 为 `null`。
- `blocks` 通过 `line_ids` 引用原始识别行，包含标题、段落、题号、小问、选项、公式和图注的语义角色。继续用原有 `line_id` 调用 `edit`。文字类别保留在行的 `kind` 字段。
- `figure_assets` 提供截图类型、框选来源和关联图注的行 ID。公式与表格截图的 `role` 分别为 `formula_crop` 和 `table_crop`，未能细分的截图为 `figure`。原生表格和公式仍通过 `objects` 交接。
- `text_counts` 记录各文字类别的总量、纳入量和排除量。`inspect` 按全部可用文字统计。`export` 按所选内容范围和 `--text-kind` 统计分类结果，图表截图内的文字由裁切处理。
- `structure_review` 提供待核对的公式与表格，字段包括原图 `bounds`、`format`、`kind`、草稿 `value`、`method` 和 `reason`。空 `value` 表示只能依据原图录入；其他草稿也须逐项核对。
- 确定整张图都是印刷体时，可在 `recognize` 或 `retry` 中明确传入 `--assume-printed`，将本次识别行标为印刷体。

已识别工作稿中的图表和公式截图可独立导出。先准备一个输出文件夹，命令会返回每张截图的页码位置、路径和关联的图注行。

```powershell
& $ocr figures ".\识别工作稿.ocrproj" --output-dir ".\截图"
```

已核对的对象可通过 `confirm-objects` 写入新工作稿。每项使用 `inspect` 返回的 `page_id` 和对应转正后原图坐标。格式为 `原生公式`、`正文与行内公式` 或 `可编辑表格`。下面是行内公式的确认文件样例。

```json
{
  "schema_version": 1,
  "objects": [
    {
      "page_id": "从 inspect 取得的页面 ID",
      "bounds": [120, 200, 510, 246],
      "format": "正文与行内公式",
      "kind": "printed",
      "value": "求证：$x_{1}+x_{2}>2$。"
    }
  ]
}
```

```powershell
& $ocr confirm-objects ".\识别工作稿.ocrproj" --objects ".\确认对象.json" --output ".\公式与表格已确认.ocrproj"
```

可用 `--layout handout` 导出 A4 单栏讲义。可分别加 `--cover` 和 `--contents`，用 `--title` 设置讲义标题。旧调用继续使用贴近原图的布局，`--pdf-style photo` 生成照片留档版。装有 LibreOffice 时，`--pdf-engine word` 可用 Word 文档转换出同版 PDF。

```powershell
& $ocr export ".\识别工作稿.ocrproj" --layout handout --title "数学复习讲义" --cover --contents --docx ".\讲义.docx" --pdf ".\讲义.pdf"
```

导出的 JSON 在 `layout_checks` 中按文件类型给出执行的检查、已核验的页数和问题列表。问题可能包含输出页码、来源页面 ID 和原图像素框，具体以实际执行的检查为准。
- 新版页面含 `orientation.clockwise`、`orientation.input_size`、`input_scan_area` 和文字的 `input_bounds`。原有 `bounds` 等字段对应工作稿中的当前图像，`input_*` 对应应用额外旋转前的 EXIF 校正图像。旧工作稿的参考图像以其保存时的图片为准。
- `quality` 区分 `ok`、`blank`、`no_text`、`low_quality`、`direction_uncertain` 等状态，附原因和建议。整批处理中需要复核的页列在顶层 `warnings`。有警告仍可成功返回 JSON，调用方可据此决定是否继续导出。

只重试某一页时，例如第 2 页需要再旋转 180°，使用：

~~~powershell
& $ocr retry ".\识别工作稿.ocrproj" --page 2 --rotation 180 --output ".\重试后.ocrproj"
~~~

`retry` 重识别指定页的整图，角度相对于工作稿当前页面。原工作稿保留，其他页可复用原识别结果。若该页文字已校对，建议先核对新旧结果再使用新工作稿。

## 校对并导出文档

Agent 可按识别结果修订文字或类别。把以下 JSON 保存为 `修改.json`，将示例里的 `line_id` 换成结果中的完整值。每项可提供 `text`、`kind`，也可以两项都提供。

```json
{
  "schema_version": 1,
  "edits": [
    {"line_id": "替换为结果里的完整ID", "text": "校对后的文字", "kind": "printed"}
  ]
}
```

```powershell
& $ocr edit ".\识别工作稿.ocrproj" --edits ".\修改.json" --output ".\校对后.ocrproj"
& $ocr export ".\校对后.ocrproj" --docx ".\整理结果.docx" --pdf ".\整理结果.pdf"
```

- 校对保留原文字框和图表位置。修改过的文字块会获得新 `line_id`，下一轮请使用最新 JSON。过期或未知的 ID 会使整批修改失败。
- `export` 也可以直接使用原工作稿。可只导出 Word 或 PDF。PDF 默认白底黑字，`--pdf-style photo` 生成照片版。加 `--text-kind printed` 或 `--text-kind handwritten` 可按类别导出。
- 默认按工作稿的识别页导出。工作稿已有错题集时，可用 `--scope collection` 导出保存的选题快照。编辑原页文字后，原有选题快照仍使用收录时保存的内容。
- 编辑后的工作稿可以在桌面软件里继续校对。Word 和 PDF 可作为 Agent 后续文档编辑的输入。

## 给调用方

调用成功时，退出码为 0，标准输出是一份 UTF-8 JSON。处理失败时，错误写到标准错误并返回非零退出码。模型初始化或诊断日志也写到标准错误。输出文件请放在已经存在的目录里，并使用新文件名。

在其他工作目录调用时，将 `$ocr` 设为可执行文件或启动脚本的绝对路径。相对输入和输出路径以调用方的当前目录为准。可通过 `& $ocr --help` 或 `& $ocr recognize --help` 查看参数。

## 批量恢复与照片分享版

- 为 `recognize` 或 `retry` 添加 `--progress` 可以查看逐页进度。进度写入标准错误，标准输出仍是一份 JSON。
- `retry --unfinished --output 新工作稿.ocrproj` 仅处理未识别页和失败页。结果的 `retry_summary` 包含处理数量和逐页失败详情。
- 用 `export --pdf-style photo --pdf-dpi 150` 生成照片分享版。省略 `--pdf-dpi` 时保留原分辨率。新版能读取旧版工作稿。包含去重图片引用或新增对象格式的工作稿需要新版读取。

讲义的 `blocks[].role` 可包含 `list_item`（编号知识点）与 `callout_label`（栏目提示）。正文的源行和 `line_id` 校对协议保持兼容。桌面为页面设置的讲义标题随工作稿提供给导出器。按类别导出导致大量文字被排除时，`layout_checks` 给出 `content_filtered`，包含纳入、排除行数及因筛选而省略的来源。照片 PDF 保留原图。

`inspect --document-flow` 可在原有 JSON v1 结果后附加 A4 讲义文档流。行内片段按正文读取顺序列出，并使用原图 `bounds` 和现有 `line_ids` 定位来源。状态 `confirmed` 表示已核对且有可编辑的公式结构，`text_only` 表示目前作为可搜索文字，`needs_review` 表示待对照原图，`text` 表示普通正文。识别置信度是源 OCR 行的分数。一个确认选区包含多段时，片段共用该选区坐标。

在 `export` 后加 `--asset-audit` 可得到按文档格式分列的 `asset_audit`。每个资产含 `asset_id`、旋转校正后原图的 `bounds`，拼合图还列出 `parts`。单元格截图给出所在表格的范围和从 1 开始的行列号。原生表格的 `replacements` 记录可能由该表格取代的检测区域及仍保留的单元格裁切 ID。原生表格可使用 `@crop(左,上,右,下)` 作为单元格内容，坐标以表格框的宽高归一化为 0–1000。
