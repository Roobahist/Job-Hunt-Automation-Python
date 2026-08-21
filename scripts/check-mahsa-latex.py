from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from job_hunt.rendering.latex import PdfLatexCompiler
from job_hunt.rendering.profiles import MahsaCvRenderer


def compile_case(name: str, data: dict[str, object]) -> None:
    root = Path("tenants/mahsa")
    template = (root / "templates/cv_template.tex").read_text()
    rendered = MahsaCvRenderer().render(template, data)

    with tempfile.TemporaryDirectory(prefix=f"mahsa-latex-{name}-") as tmp:
        tex_path = Path(tmp) / "MahsaAzar_CV.tex"
        tex_path.write_text(rendered)
        pdf_path = PdfLatexCompiler(timeout_seconds=180).compile(tex_path)
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            raise RuntimeError(f"{name}: pdflatex did not produce a non-empty PDF")


def main() -> None:
    root = Path("tenants/mahsa")
    master = json.loads((root / "master_cv.json").read_text())
    compile_case("master", master)

    empty_references = copy.deepcopy(master)
    for section in empty_references["sections"]:
        if section.get("type") == "references":
            section["items"] = []
    rendered_empty = MahsaCvRenderer().render(
        (root / "templates/cv_template.tex").read_text(),
        empty_references,
    )
    if "\\CVReferences{" in rendered_empty:
        raise RuntimeError("empty references must not emit a CVReferences block")
    compile_case("empty-references", empty_references)

    print("Mahsa LaTeX smoke tests passed")


if __name__ == "__main__":
    main()
