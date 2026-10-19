#!/usr/bin/env python3
"""scan2md — turn a scanned PDF into Markdown, resumably.

A scanned book has no text layer, so every page must be rasterised and OCR'd.
That costs seconds per page; a 350-page book is an hour of work. This tool is
built around that fact: each page's result is checkpointed, so an interrupted
run resumes instead of restarting.

The page-level recognition strategy is adapted from a working pipeline written
in the same user's other workspace (see NOTICE for provenance):
  * bleed-through is killed by measuring the share of near-black pixels inside
    each recognised box on the ORIGINAL greyscale raster;
  * recognised boxes are clustered into visual lines by their vertical centre;
  * running headers/footers are removed by document-wide frequency plus a band
    test, because they repeat and the margins vary per book;
  * section numbers are promoted to Markdown headings only when they carry a
    dot, since a bare integer at line start is usually a misread Greek symbol
    in a nomenclature list.

Exit codes
  0  success
  2  the document already has a text layer — use dsh-pdf2md instead
  3  missing dependency (rapidocr_onnxruntime / pymupdf / numpy)
  4  bad input

Commands
  probe    <pdf>                      is this actually scanned?
  convert  <pdf> --out <md> [opts]    OCR to Markdown, resumable
  report   <job-dir>                  quality summary from checkpoints
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from pathlib import Path

EXIT_OK = 0
EXIT_HAS_TEXT_LAYER = 2
EXIT_MISSING_DEP = 3
EXIT_BAD_INPUT = 4

# Measured on a 300 dpi book scan: ghost text sits near 0.0% dark pixels,
# the faintest genuine line (a bare equation number) around 3.7%.
GHOST_DARK_RATIO = 0.015
GHOST_SCORE = 0.90
MIN_SCORE_SHORT_CJK = 0.62
MIN_CJK_FOR_SHORT_TEXT = 4

TOP_BAND = 0.12
BOT_BAND = 0.85

NUM_HEAD = re.compile(
    r"^(\d{1,2}(?:\.\d{1,3}){1,4})(?![.\d])\s*[、.．]?\s*([\u4e00-\u9fffA-Za-z].*)$"
)
CHAP_HEAD = re.compile(r"^(第[一二三四五六七八九十百零〇\d]{1,4}章)\s*(\S.*)$")


def _require(module: str, hint: str):
    try:
        return __import__(module)
    except ImportError:
        print(f"error: {module} is not installed for {sys.executable}\n  fix: {hint}", file=sys.stderr)
        raise SystemExit(EXIT_MISSING_DEP)


def cjk_len(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def render_gray(pymupdf, page, dpi: int):
    import numpy as np

    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)


def box_dark_ratio(g, box) -> float:
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    x0, x1 = int(max(0, min(xs))), int(min(g.shape[1], max(xs)))
    y0, y1 = int(max(0, min(ys))), int(min(g.shape[0], max(ys)))
    if x1 <= x0 or y1 <= y0:
        return 1.0
    region = g[y0:y1, x0:x1]
    return float((region < 128).mean())


def ocr_page(engine, g) -> list[dict]:
    """Recognise one page and return visual lines, filtered for bleed-through."""
    import numpy as np

    result, _ = engine(np.stack([g] * 3, axis=-1))
    if not result:
        return []

    height = g.shape[0]
    items = []
    for box, text, score in result:
        text = (text or "").strip()
        if not text:
            continue
        dark = box_dark_ratio(g, box)
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        if dark < GHOST_DARK_RATIO and float(score) < GHOST_SCORE:
            continue  # bleed-through from the reverse side
        if float(score) < MIN_SCORE_SHORT_CJK and cjk_len(text) < MIN_CJK_FOR_SHORT_TEXT:
            continue  # mangled equation fragment
        yc = (min(ys) + max(ys)) / 2.0
        items.append(
            {
                "x0": min(xs),
                "x1": max(xs),
                "yc": yc,
                "h": max(ys) - min(ys),
                "text": text,
                "score": float(score),
                "top": yc / height,
            }
        )

    items.sort(key=lambda d: d["yc"])
    lines: list[dict] = []
    for item in items:
        if lines and abs(item["yc"] - lines[-1]["yc"]) < 0.55 * max(item["h"], lines[-1]["h"], 1):
            lines[-1]["items"].append(item)
        else:
            lines.append({"yc": item["yc"], "h": item["h"], "items": [item]})

    out = []
    for line in lines:
        line["items"].sort(key=lambda d: d["x0"])
        avg_h = sum(i["h"] for i in line["items"]) / len(line["items"])
        parts = []
        prev_x1 = None
        for index, item in enumerate(line["items"]):
            if index and prev_x1 is not None and item["x0"] - prev_x1 > 0.9 * avg_h:
                parts.append(" ")
            parts.append(item["text"])
            prev_x1 = item["x1"]
        text = "".join(parts).strip()
        if text:
            out.append(
                {
                    "text": text,
                    "top": line["yc"] / height,
                    "score": min(i["score"] for i in line["items"]),
                }
            )
    return out


def promote_heading(text: str) -> str | None:
    if len(text) > 40:
        return None
    match = CHAP_HEAD.match(text)
    if match:
        return f"# {match.group(1)} {match.group(2)}".strip()
    match = NUM_HEAD.match(text)
    if match:
        rest = re.sub(r"\s+\d{1,4}$", "", match.group(2)).strip()
        if not rest or len(rest) > 30 or re.search(r"[=<>|]", rest):
            return None
        depth = min(match.group(1).count(".") + 2, 5)
        return f"{'#' * depth} {match.group(1)} {rest}".strip()
    return None


# --- checkpointing -----------------------------------------------------------

def job_paths(job_dir: Path) -> dict[str, Path]:
    return {
        "pages": job_dir / "pages",
        "progress": job_dir / "progress.jsonl",
        "state": job_dir / "state.json",
        "report": job_dir / "quality.json",
    }


def read_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def page_file(paths: dict[str, Path], page_no: int) -> Path:
    return paths["pages"] / f"{page_no:04d}.json"


def append_progress(paths: dict[str, Path], record: dict) -> None:
    paths["progress"].parent.mkdir(parents=True, exist_ok=True)
    with paths["progress"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- commands ----------------------------------------------------------------

def probe_pdf(pdf: Path, max_pages: int = 6) -> dict:
    pymupdf = _require("pymupdf", "<python> -m pip install pymupdf")
    try:
        doc = pymupdf.open(str(pdf))
    except Exception as exc:  # noqa: BLE001
        print(f"error: cannot open {pdf}: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_BAD_INPUT)

    try:
        sample = min(doc.page_count, max_pages)
        chars = 0
        images = 0
        for index in range(sample):
            chars += len((doc[index].get_text("text") or "").strip())
            try:
                images += len(doc[index].get_images(full=True))
            except Exception:
                pass
        pages = doc.page_count
    finally:
        doc.close()

    per_page = chars / max(sample, 1)
    scanned = per_page < 40
    return {
        "path": str(pdf),
        "pages": pages,
        "sampled_pages": sample,
        "chars_in_sample": chars,
        "median_chars_per_page": round(per_page, 1),
        "images_in_sample": images,
        "verdict": "needs_ocr" if scanned else "text_layer",
        "recommendation": (
            "this tool (scan2md) — pages have no text layer"
            if scanned
            else "dsh-pdf2md — the document already has a text layer, OCR would only add errors"
        ),
    }


def cmd_probe(args) -> int:
    info = probe_pdf(Path(args.pdf))
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return EXIT_OK if info["verdict"] == "needs_ocr" else EXIT_HAS_TEXT_LAYER


def cmd_convert(args) -> int:
    pymupdf = _require("pymupdf", "<python> -m pip install pymupdf")
    _require("numpy", "<python> -m pip install numpy")
    rapidocr = _require(
        "rapidocr_onnxruntime",
        "<python> -m pip install rapidocr_onnxruntime  (CN mirror: -i https://pypi.tuna.tsinghua.edu.cn/simple)",
    )

    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"error: no such file: {pdf}", file=sys.stderr)
        return EXIT_BAD_INPUT

    info = probe_pdf(pdf)
    if info["verdict"] != "needs_ocr" and not args.force:
        print(json.dumps({**info, "refused": "already has a text layer"}, ensure_ascii=False, indent=2))
        return EXIT_HAS_TEXT_LAYER

    out = Path(args.out)
    job_dir = Path(args.job_dir) if args.job_dir else out.with_suffix(".scan2md")
    paths = job_paths(job_dir)
    paths["pages"].mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(str(pdf))
    total = doc.page_count
    start = max(1, args.start or 1)
    end = min(total, args.end or total)

    state = read_state(paths["state"])
    state.update(
        {
            "pdf": str(pdf),
            "out": str(out),
            "dpi": args.dpi,
            "start": start,
            "end": end,
            "pages_total": total,
            "resumed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    write_state(paths["state"], state)

    engine = rapidocr.RapidOCR()
    started = time.time()
    done = 0
    reused = 0

    for page_no in range(start, end + 1):
        target = page_file(paths, page_no)
        if args.resume and target.exists():
            reused += 1
            continue

        t0 = time.time()
        gray = render_gray(pymupdf, doc[page_no - 1], args.dpi)
        lines = ocr_page(engine, gray)
        elapsed = time.time() - t0
        target.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
        append_progress(
            paths,
            {
                "page": page_no,
                "lines": len(lines),
                "seconds": round(elapsed, 1),
                "done": done + reused + 1,
                "total": end - start + 1,
            },
        )
        done += 1
        if args.verbose:
            print(f"p{page_no}/{end} lines={len(lines)} {elapsed:.1f}s", flush=True)

    doc.close()

    # Assembly needs every page (chrome detection is document-wide), so it runs
    # after the loop and is cheap to repeat.
    lines_by_page: dict[int, list[dict]] = {}
    for page_no in range(start, end + 1):
        target = page_file(paths, page_no)
        if target.exists():
            try:
                lines_by_page[page_no] = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                lines_by_page[page_no] = []
        else:
            lines_by_page[page_no] = []

    freq: collections.Counter = collections.Counter()
    for lines in lines_by_page.values():
        for line in lines:
            freq[line["text"]] += 1

    page_count = len(lines_by_page)

    def is_chrome(line: dict) -> bool:
        text = line["text"]
        in_band = line["top"] < TOP_BAND or line["top"] > BOT_BAND
        if freq[text] >= max(3, page_count * 0.20) and in_band:
            return True
        if re.fullmatch(r"[\d\-—·.、\s]{1,8}", text) and in_band:
            return True
        return False

    md: list[str] = []
    if args.title:
        md.append(f"# {args.title}\n")
    md.append(
        f"> 来源：`{pdf.name}`　页 {start}–{end}　OCR：RapidOCR(PP-OCR) @ {args.dpi}dpi　"
        f"透印过滤：暗像素<{GHOST_DARK_RATIO:.1%}\n"
    )

    low_confidence: list[dict] = []
    empty_pages: list[int] = []
    for page_no in sorted(lines_by_page):
        body = [line for line in lines_by_page[page_no] if not is_chrome(line)]
        if not body:
            empty_pages.append(page_no)
            continue
        md.append(f"<!-- page {page_no} -->")
        for line in body:
            heading = promote_heading(line["text"])
            md.append(heading if heading else line["text"])
            if line.get("score", 1.0) < 0.70:
                low_confidence.append({"page": page_no, "score": round(line["score"], 2), "text": line["text"][:60]})
        md.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md) + "\n", encoding="utf-8")

    report = {
        "pdf": str(pdf),
        "out": str(out),
        "pages": end - start + 1,
        "pages_ocrd_now": done,
        "pages_reused": reused,
        "empty_pages": empty_pages[:50],
        "empty_page_count": len(empty_pages),
        "low_confidence_count": len(low_confidence),
        "low_confidence_sample": low_confidence[:30],
        "chars": sum(len(x) for x in md),
        "seconds": round(time.time() - started, 1),
        "engine": "RapidOCR(PP-OCR)",
        "dpi": args.dpi,
    }
    paths["report"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_report(args) -> int:
    paths = job_paths(Path(args.job_dir))
    if not paths["report"].exists():
        print(f"error: no report at {paths['report']} — run convert first", file=sys.stderr)
        return EXIT_BAD_INPUT
    print(paths["report"].read_text(encoding="utf-8"))
    return EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="scan2md", description="OCR a scanned PDF into Markdown, resumably.")
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="decide whether this PDF needs OCR at all")
    probe.add_argument("pdf")
    probe.set_defaults(func=cmd_probe)

    convert = sub.add_parser("convert", help="OCR to Markdown (per-page checkpoints make this resumable)")
    convert.add_argument("pdf")
    convert.add_argument("--out", required=True, help="Markdown file to write")
    convert.add_argument("--job-dir", help="checkpoint directory (default: <out>.scan2md)")
    convert.add_argument("--title", default="", help="H1 title for the assembled document")
    convert.add_argument("--dpi", type=int, default=300)
    convert.add_argument("--start", type=int, default=1)
    convert.add_argument("--end", type=int, default=0, help="0 = last page")
    convert.add_argument("--resume", action="store_true", help="reuse existing page checkpoints")
    convert.add_argument("--force", action="store_true", help="OCR even if a text layer exists")
    convert.add_argument("--verbose", action="store_true")
    convert.set_defaults(func=cmd_convert)

    report = sub.add_parser("report", help="print the quality report of a finished job")
    report.add_argument("job_dir")
    report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
