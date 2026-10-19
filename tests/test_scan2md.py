"""Black-box tests for scan2md.

Fixtures are generated at test time — the repository holds no binary files.
Text in fixtures is ASCII on purpose: PyMuPDF's built-in fonts carry no CJK
glyphs, so Chinese fixture text would rasterise as boxes and prove nothing.

Run:  python -m pytest tests -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "scan2md" / "scripts" / "scan2md.py"

pymupdf = pytest.importorskip("pymupdf", reason="PyMuPDF is needed to build fixtures")
pytest.importorskip("numpy", reason="NumPy is a runtime dependency")
pytest.importorskip("rapidocr_onnxruntime", reason="RapidOCR is the engine under test")

BODY = "The OCR pipeline should read this paragraph back. " * 4  # > 40 chars/page


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )


@pytest.fixture(scope="module")
def text_pdf(tmp_path_factory) -> Path:
    """A PDF that already has a text layer, with a realistic amount of text."""
    target = tmp_path_factory.mktemp("text") / "text.pdf"
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 90, 520, 700), BODY, fontsize=12)
    doc.save(str(target))
    doc.close()
    return target


@pytest.fixture(scope="module")
def scanned_pdf(tmp_path_factory) -> Path:
    """The same kind of page, flattened to an image: no text layer at all."""
    work = tmp_path_factory.mktemp("scan")
    source = pymupdf.open()
    page = source.new_page()
    page.insert_textbox(pymupdf.Rect(72, 90, 520, 400), BODY, fontsize=16)
    png = work / "page.png"
    page.get_pixmap(dpi=150).save(str(png))

    images = pymupdf.open()
    fresh = images.new_page(width=page.rect.width, height=page.rect.height)
    fresh.insert_image(fresh.rect, filename=str(png))
    target = work / "scanned.pdf"
    images.save(str(target))
    images.close()
    source.close()
    return target


# --- probe ------------------------------------------------------------------

def test_probe_identifies_scanned(scanned_pdf: Path) -> None:
    result = run("probe", str(scanned_pdf))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["verdict"] == "needs_ocr"


def test_probe_identifies_text_layer(text_pdf: Path) -> None:
    result = run("probe", str(text_pdf))
    # Exit 2 is the documented "do not OCR this" signal.
    assert result.returncode == 2, result.stdout
    assert json.loads(result.stdout)["verdict"] == "text_layer"


# --- refusal ----------------------------------------------------------------

def test_convert_refuses_text_layer(text_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "refused.md"
    result = run("convert", str(text_pdf), "--out", str(out))
    assert result.returncode == 2, result.stdout
    assert not out.exists(), "a refused conversion must not leave a file behind"


# --- conversion + resume ----------------------------------------------------

@pytest.fixture(scope="module")
def converted(scanned_pdf: Path, tmp_path_factory):
    work = tmp_path_factory.mktemp("run")
    out = work / "book.md"
    job = work / "job"
    first = run(
        "convert", str(scanned_pdf), "--out", str(out), "--job-dir", str(job), "--title", "OCR test"
    )
    assert first.returncode == 0, first.stderr
    # The second run must pass the same options: it reassembles from checkpoints
    # and would otherwise drop the title.
    second = run(
        "convert",
        str(scanned_pdf),
        "--out",
        str(out),
        "--job-dir",
        str(job),
        "--title",
        "OCR test",
        "--resume",
    )
    assert second.returncode == 0, second.stderr
    return {
        "out": out,
        "job": job,
        "first": json.loads(first.stdout),
        "second": json.loads(second.stdout),
    }


def test_conversion_produces_markdown(converted) -> None:
    body = converted["out"].read_text(encoding="utf-8")
    assert "# OCR test" in body
    assert "<!-- page 1 -->" in body
    assert converted["first"]["pages_ocrd_now"] == 1
    assert converted["first"]["chars"] > 0
    # The recognised line must contain real words, not an empty page.
    assert "OCR" in body.upper()


def test_resume_reuses_checkpoints(converted) -> None:
    second = converted["second"]
    assert second["pages_ocrd_now"] == 0, "resume must not re-OCR finished pages"
    assert second["pages_reused"] == 1


def test_progress_log_has_one_line_per_page(converted) -> None:
    lines = (converted["job"] / "progress.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["page"] == 1
    assert record["total"] == 1


def test_report_is_machine_readable(converted) -> None:
    result = run("report", str(converted["job"]))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["pages"] == 1
    assert "low_confidence_count" in report


def test_report_missing_job_fails(tmp_path: Path) -> None:
    result = run("report", str(tmp_path / "nope"))
    assert result.returncode == 4
