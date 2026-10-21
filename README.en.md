# dsh-scan2md

[中文说明](README.md)

Turns a **scanned PDF** — a photocopied or photographed book with no text layer —
into Markdown. Resumable, page-addressable, and honest about what OCR damages.

## What it is for

A scanned book is a stack of page images: nothing can be selected, copied, or
searched. Any pipeline that wants text out of it needs OCR.

This skill does that, and it is built for the one fact that defines the job:
**a book takes about an hour.** At roughly 10 seconds a page, a 350-page volume
is ~58 minutes of work. Naive OCR scripts keep everything in memory and write the
file at the end — so a crash at minute 55 loses all of it.

Three properties follow from that:

| Problem | Answer |
|---|---|
| The run died after 40 minutes | every page is checkpointed; `--resume` continues |
| You cannot tell how far along it is | `progress.jsonl`, one JSON line per page |
| You do not know which pages to trust | `quality.json` names empty pages and low-confidence lines |

## What it refuses

```bash
python scripts/scan2md.py probe book.pdf
```

`probe` is read-only and takes seconds. If the document **already has a text
layer**, `convert` exits 2 and writes nothing, unless you pass `--force`.

That refusal is the point. OCR of clean text is strictly worse than extraction:
slower, costlier, and it introduces errors where there were none. Use
[`dsh-pdf2md`](https://www.npmjs.com/package/dsh-pdf2md) for those documents.

## Install

### As a DSH plugin (recommended)

```bash
dsh plugin --profile web add dsh-scan2md
```

Desktop clients use their own profile name (the default is `desktop`):

```bash
dsh plugin --profile desktop add dsh-scan2md
```

`dsh plugin add` installs the package into the profile **and registers it in `dsh.profile.bundles` automatically** — verified on a fresh profile, where one command added the entry. Restart DSH and `scan2md` appears in the skill catalog.

Without the `dsh` command, `pnpm add dsh-scan2md` plus a manual entry in the profile's `dsh.profile.bundles` works too.

### Python dependencies (required; Python 3.11–3.12)

Python 3.11–3.12 with `pymupdf`, `numpy`, `rapidocr_onnxruntime`:

```bash
<python> -m pip install -r skills/scan2md/requirements.txt
```

**Size and network use** (measured, not estimated):

- **The OCR models ship inside the package, so `pip install` fetches them and the
  first run does not go online.** `rapidocr_onnxruntime` bundles three PP-OCRv4
  models — detection 4.5 MB, recognition 10.4 MB, orientation 0.6 MB,
  **15.4 MB in total** (the wheel itself is 14.2 MB).
- The bulk is in its dependencies: on Windows the five main packages come to
  roughly **142 MB** of download — `pymupdf` ~60 MB, `opencv-python` ~42 MB,
  `onnxruntime` ~14 MB, `rapidocr_onnxruntime` ~14 MB, `numpy` ~12 MB. Other
  platforms are in the same range.
- So: **one slow install, then no network at all.** If a corporate proxy needs an
  allow-list entry, it is `pypi.org` (or your mirror) — there is no separate
  model-download host to open.

**The Python version has a ceiling.** `rapidocr_onnxruntime` declares
`requires_python <3.13`, so this stack **will not install on Python 3.13** — pip
refuses it, it is not merely untested. CI covers **3.11 and 3.12**, which are the
versions actually exercised. 3.10 is not in the matrix: it should work, but it has
not been run, so it is not claimed.

Verify before a long run:

```bash
<python> -c "import pymupdf, numpy, rapidocr_onnxruntime; print('ok')"
```

## Usage

```bash
# 1. does this book need OCR at all?
python scripts/scan2md.py probe book.pdf

# 2. convert — takes tens of minutes; safe to interrupt
python scripts/scan2md.py convert book.pdf --out book.md --resume --verbose

# 3. what should I distrust?
python scripts/scan2md.py report book.scan2md
```

| Option | Meaning |
|---|---|
| `--out` | Markdown file to write (required) |
| `--job-dir` | checkpoint directory, default `<out>.scan2md` |
| `--start` / `--end` | page range — run a few pages before committing to a whole book |
| `--resume` | reuse checkpoints (**always pass this when re-running**) |
| `--dpi` | default 300; below 200 loses CJK strokes |
| `--force` | OCR even if a text layer exists |

## What OCR cannot give you

- **Formulas are damaged.** Subscripts, superscripts, fractions, integrals come
  out as fragments. Anything quantitative must be checked against the scan.
- **Greek letters are the worst case.** `φ` becomes `中`, `λ` becomes `入`. A
  nomenclature page is not a reliable source for symbol meaning.
- **Two-column layouts interleave.**
- **Errors are silent.** The text reads smoothly and is still wrong.

For book distillation or RAG ingestion: OCR text is a **reading aid**, not a
source of record. Keep the `<!-- page N -->` anchors — they are what makes a
claim checkable.

## Requirements

See [Python dependencies](#python-dependencies-required-python-310) above — the
same three packages, Python 3.10+, models included in the wheel.

The usual failure is installing the OCR package into a different interpreter than
the one that has `pymupdf`. Verify before a long run:

```bash
<python> -c "import pymupdf, numpy, rapidocr_onnxruntime; print('ok')"
```

## Licence

MIT for this package. Dependencies keep their own terms: **PyMuPDF is AGPL-3.0**
(installed by the user, not distributed here), RapidOCR is Apache-2.0.
