# dsh-scan2md

把**扫描件 PDF**（没有文字层、拍照或复印的书）OCR 成 Markdown：**可断点续跑**，带**逐页溯源**和**质量报告**。

A DSH skill that OCRs scanned PDFs into Markdown — resumably, with per-page
provenance, and honest about what OCR damages.

## 为什么做这个

`dsh-pdf2md` 遇到扫描件会**拒绝**（exit 2），因为写一个空文件比报错更糟。但拒绝之后你得有下一步——这个 skill 就是那一步。

它和一般的 OCR 脚本差在三件事：

| | 一般脚本 | 这个 |
|---|---|---|
| 351 页跑到一半崩了 | 从头再来 | `--resume` 复用已完成页，零成本 |
| 不知道跑到哪了 | 看屏幕 | `progress.jsonl` 每页一行，随时读 |
| 不知道哪页不可信 | 靠猜 | `quality.json` 列出低置信行和空白页 |

## 安装

### 作为 DSH 插件（推荐）

```bash
dsh plugin --profile web add dsh-scan2md
```

桌面客户端把 profile 名换成你的（默认 `desktop`）：

```bash
dsh plugin --profile desktop add dsh-scan2md
```

`dsh plugin add` 会把包装进 profile，**并自动登记到 `dsh.profile.bundles`**（实测：全新 profile 一条命令，bundles 就多出这一项）。重启后技能清单出现 `scan2md`。

不用 `dsh` 命令的话，也可以 `pnpm add dsh-scan2md`，再手动把包名加进 profile `package.json` 的 `dsh.profile.bundles`。

### Python 依赖（必须；Python 3.11–3.12）

```bash
<python> -m pip install -r skills/scan2md/requirements.txt
# 国内加镜像
<python> -m pip install -r skills/scan2md/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**体积与联网说明**（实测，不是估计）：

- **OCR 模型打在包里，`pip install` 时就下完了，首次运行不再联网。** `rapidocr_onnxruntime` 自带 PP-OCRv4 三个模型——检测 4.5 MB、识别 10.4 MB、方向分类 0.6 MB，**合计 15.4 MB**（wheel 本身 14.2 MB）。
- 真正占体积的是它的依赖，Windows 下五个主包合计约 **142 MB** 下载量：`pymupdf` 约 60 MB、`opencv-python` 约 42 MB、`onnxruntime` 约 14 MB、`rapidocr_onnxruntime` 约 14 MB、`numpy` 约 12 MB。其余平台量级相近。
- 所以：**装一次要等一会儿，但装完之后跑多久都不再联网。** 如果公司网络需要白名单，放行的是 `pypi.org`（或镜像站），不需要额外的模型下载域名。

**Python 版本是有上界的**：`rapidocr_onnxruntime` 声明 `requires_python <3.13`，所以**这套依赖在 Python 3.13 上装不上**，不是"没测过"，是 pip 直接拒绝。CI 覆盖 **3.11 / 3.12** 两个版本，这两档是实测可用的。3.10 未在 CI 覆盖——理论上应当可用，但没跑过就不写进承诺。

验证：

```bash
<python> -c "import pymupdf, numpy, rapidocr_onnxruntime; print('ok')"
```

## 用法

```bash
# 1. 先判断：这书到底要不要 OCR
python scripts/scan2md.py probe book.pdf

# 2. 转（一本书几十分钟，中途可以断）
python scripts/scan2md.py convert book.pdf --out book.md --resume --verbose

# 3. 看质量
python scripts/scan2md.py report book.jobdir
```

| 参数 | 作用 |
|---|---|
| `--out` | 输出的 Markdown 文件（必填） |
| `--job-dir` | 检查点目录，默认 `<out>.scan2md` |
| `--start` / `--end` | 页码范围 |
| `--resume` | 复用已有检查点（**重跑时务必加**） |
| `--dpi` | 渲染分辨率，默认 300（低于 200 中文笔画会丢） |
| `--force` | 已有文字层也强行 OCR |

### 实测速度

第 2 本书（204 页）前 3 页：**30 秒**，约 **10 秒/页**。

- 204 页 ≈ 34 分钟
- 351 页 ≈ 58 分钟

所以：不要让这个任务阻塞对话，起后台，然后轮询 `progress.jsonl`。

## 它到底做了什么

- **透印**：按识别框内暗像素比例过滤（<1.5% 且置信度 <0.90 判为背面透字）
- **行聚簇**：按垂直中心聚类，同行按左到右拼接，间距 >0.9 倍行高才加空格
- **页眉页脚**：全文词频（≥20% 页重复）+ 上下 12%/15% 区域
- **标题识别**：只有带小数点的编号才升为标题（裸整数多半是符号表里的希腊字母误读）
- **页码可引用**：`<!-- page 18 -->`

## 它做不到什么（务必转告下游）

- **公式会坏**：上下标、分式、积分都变成碎片。任何定量结论必须回原书核对
- **希腊字母最差**：`φ` 变 `中`，`λ` 变 `入`。符号表不能作为符号含义的依据
- **双栏可能串行**
- **错误是静默的**：读起来通顺，但可能错。引用时要能定位到页

**用于拆书/蒸馏时，OCR 文本是阅读辅助，不是引用来源。**

## 沙箱提示

子进程常被限制在工作区内写文件。工作区外 `PermissionError` 是沙箱边界，不是 skill 坏了——输出放进工作区。

## 开发

```bash
node --check lib/startup.js
python -m py_compile skills/scan2md/scripts/scan2md.py
```

## 许可

MIT。依赖各自独立：**PyMuPDF 是 AGPL-3.0**（不随本包分发，由用户安装），RapidOCR 是 Apache-2.0。
