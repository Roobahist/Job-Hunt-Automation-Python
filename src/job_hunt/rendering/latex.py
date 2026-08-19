from __future__ import annotations

import re
import subprocess
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from job_hunt.errors import DocumentRenderingError

LATEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape_latex(value: object) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    return "".join(LATEX_ESCAPES.get(character, character) for character in text)


def latex_value(value: object) -> str:
    if isinstance(value, Mapping):
        allowed = {"text", "format"}
        unknown = set(value) - allowed
        if unknown:
            raise DocumentRenderingError(f"Unknown text-format keys: {sorted(unknown)}")
        text = value.get("text", "")
        if value.get("format", "text") == "latex":
            return str(text)
        return escape_latex(text)
    return escape_latex(value)


def inject_once(template: str, replacements: Mapping[str, str]) -> str:
    output = template
    for marker, content in replacements.items():
        count = output.count(marker)
        if count != 1:
            raise DocumentRenderingError(
                f"Template marker {marker!r} must occur once; found {count}"
            )
        output = output.replace(marker, content)
    unresolved = re.findall(r"%%__[A-Z0-9_]+__%%", output)
    if unresolved:
        raise DocumentRenderingError(f"Unresolved template markers: {sorted(set(unresolved))}")
    return output


class TexCompiler(Protocol):
    def compile(self, tex_path: Path) -> Path: ...


class PdfLatexCompiler:
    def __init__(self, executable: str = "pdflatex", timeout_seconds: int = 120) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds

    def compile(self, tex_path: Path) -> Path:
        command = [
            self.executable,
            "-no-shell-escape",
            "-interaction=nonstopmode",
            "-halt-on-error",
            tex_path.name,
        ]
        for _ in range(2):
            try:
                result = subprocess.run(
                    command,
                    cwd=tex_path.parent,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DocumentRenderingError(
                    f"LaTeX compiler failed to start or timed out: {exc}"
                ) from exc
            if result.returncode:
                output = (result.stdout + "\n" + result.stderr)[-4000:]
                raise DocumentRenderingError(f"LaTeX compilation failed:\n{output}")
        pdf = tex_path.with_suffix(".pdf")
        if not pdf.exists():
            raise DocumentRenderingError("LaTeX reported success without producing a PDF")
        for suffix in (".aux", ".log", ".out"):
            tex_path.with_suffix(suffix).unlink(missing_ok=True)
        return pdf
