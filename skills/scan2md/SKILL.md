---
name: scan2md
description: OCR a scanned or image-only PDF into Markdown, resumably, with per-page provenance and a quality report. Use when 扫描件 / 扫描版 PDF / 图片版 PDF / 拍照的书 需要转成文字, when pdf2md reports needs_ocr, or when a document has no selectable text and a downstream step (book distillation, RAG ingest, summarisation) needs text out of it. Refuses documents that already have a text layer.
metadata:
  version: "0.1.0"
---

# scan2md

## 这个技能是干什么的

**把扫描版 PDF 变成能读的文字。**

扫描版 = 每一页都是一张图，没有可复制的文字。书架上那些“影印版”“PDF 扫描件”“拍照的书”都是这一类。它们的共同特征是：用任何 PDF 阅读器选中文字，什么都选不到。

这个技能做的事：把每一页渲染成图片 → 用 OCR 认字 → 按书的排版拼回成 Markdown，页与页之间留锚点（`<!-- page 18 -->`），这样后面引用任何一句话都能定位回原书。

## 什么时候用它

用之前先问一句：**这本书有文字层吗？**

```bash
<python> scripts/scan2md.py probe book.pdf
```

| probe 说什么 | 用什么 |
|---|---|
| `needs_ocr` | **这个技能** |
| `text_layer` | 用 `pdf2md`——直接抽取，又快又准又免费 |

`convert` 遇到有文字层的 PDF 会**主动拒绝**（退出码 2），除非你加 `--force`。这个拒绝是有意的：对干净的文字做 OCR，只会把准确的文字变成可能出错的文字。

## 为什么不是“跑个 OCR 就完了”

一本书几百页，每页约 10 秒——**一小时的活**。中途断电、关窗口、进程被杀，普通脚本会从头再来。

所以这个技能的三个设计都围绕“长任务”：

| 问题 | 解法 |
|---|---|
| 跑了 40 分钟崩了 | 每页结果单独存盘，`--resume` 从崩的地方接着来 |
| 不知道跑到哪了 | `progress.jsonl` 每页一行，随时 tail |
| 不知道哪页不可信 | `quality.json` 列出空白页和低置信行 |

## 花多少时间

约 **10 秒/页**。

| 页数 | 预计 |
|---|---|
| 200 页 | 34 分钟 |
| 350 页 | 58 分钟 |

**所以它必须在后台跑，不能卡着对话。** 启动后轮询进度文件，不要假设它已经完成。

## 它做不到什么（务必转告下游）

- **公式会坏**：上下标、分式、积分变成碎片。定量结论必须回原书核对
- **希腊字母最不准**：`φ` 变 `中`、`λ` 变 `入`。符号表不能当作符号含义的依据
- **双栏会串行**
- **错误是静默的**：读起来通顺，但可能错

用于拆书、蒸馏、RAG 时，**OCR 文本是阅读辅助，不是引用来源**。

## 怎么跑

```bash
# 1. 判断要不要 OCR
<python> scripts/scan2md.py probe book.pdf

# 2. 转换（一本书几十分钟，中途可断）
<python> scripts/scan2md.py convert book.pdf --out book.md --resume --verbose

# 3. 看质量报告
<python> scripts/scan2md.py report book.scan2md
```

| 参数 | 作用 |
|---|---|
| `--out` | 输出的 Markdown 文件（必填） |
| `--job-dir` | 检查点目录，默认 `<out>.scan2md` |
| `--start` / `--end` | 页码范围，先跑几页看看质量 |
| `--resume` | 复用已有检查点（**重跑必加**） |
| `--dpi` | 渲染分辨率，默认 300；低于 200 中文笔画会丢 |
| `--force` | 有文字层也强行 OCR |

## 内部做了什么

每个识别框都要回到原始灰度图上量一次：

- **透印**（背面字透过来）：框内暗像素 <1.5% 且置信度 <0.90 → 丢弃。真实文字即使只是一个编号，也远高于这个下限
- **行聚簇**：垂直中心相差不到半行高的框归为同一行，再按左到右拼；间距超过 0.9 倍行高才插空格
- **页眉页脚**：同一串文字在 ≥20% 的页面上重复出现，且位于上 12% / 下 15% 区域 → 删除
- **标题识别**：只有带小数点的编号才升为标题。行首的裸整数多半是符号表里的希腊字母被误读
- **页码留痕**：`<!-- page 18 -->`，保证可引用

## 找一个能用的 Python

需要 **Python 3.11 或 3.12**（CI 实测范围）且装了 `pymupdf`、`numpy`、`rapidocr_onnxruntime`。

**3.13 装不上**：`rapidocr_onnxruntime` 声明 `requires_python <3.13`，pip 会直接拒绝，不是「可能不兼容」。
**模型不需要单独下载**：PP-OCRv4 的 det/rec/cls 三个模型（合计 15.4 MB）打在 `rapidocr_onnxruntime` 的 wheel 里，`pip install` 时就下完了，首次运行不再联网。

按顺序试，第一个能导入的就是：

1. `$SCAN2MD_PYTHON`
2. 当前工作区的 `.venv/Scripts/python.exe` 或 `.venv/bin/python`
3. `python3`、`python`

```bash
<python> -c "import pymupdf, numpy, rapidocr_onnxruntime; print('ok')"
```

**最常见、最容易出错的问题**：某个 Python 里装过 `pymupdf` 却没有 `rapidocr_onnxruntime`，脚本跑到一半才报错。装依赖要装进**同一个** Python：

```bash
<python> -m pip install -r skills/scan2md/requirements.txt
# 国内镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple
```

如果 pip 自己报 `No usable temporary directory`，那是环境问题：把 `TEMP`、`TMP`、`TMPDIR` 指到一个可写目录再试。

## 写在能写的地方

子进程常被限制在工作区内。工作区外报 `PermissionError` 是沙箱边界，不是技能坏了——把输出放进工作区。
