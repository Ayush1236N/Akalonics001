"""Upload safety: magic-byte sniffing, zip-bomb guards, formula/DDE stripping, caps."""
from __future__ import annotations

import io
import re
import zipfile

import pandas as pd

from config import settings
from profiling import ALLOWED_EXT, ParseError

ZIP_SIG = b"PK\x03\x04"
OLE_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
GZIP_SIG = b"\x1f\x8b"
PDF_SIG = b"%PDF"
RAR_SIG = b"Rar!"
SEVENZ_SIG = b"7z\xbc\xaf\x27\x1c"
ELF_SIG = b"\x7fELF"
PNG_SIG = b"\x89PNG"

# Excel neutralisation: leading =, @, +, - followed by something formula-ish.
_FORMULA_RE = re.compile(r"^\s*[=@]|^\s*[+\-](?=[A-Za-z(=@])")
_DDE_RE = re.compile(r"(?i)\b(cmd\s*\||dde\s*\(|msexcel\s*\||powershell|=\s*hyperlink\s*\()")


class UnsafeUpload(ParseError):
    """Rejected for safety reasons (bomb, encrypted, wrong magic bytes)."""


def sniff(filename: str, raw: bytes) -> str:
    """Return the extension we will actually parse with, based on content not name.

    Raises UnsafeUpload when the bytes are a type we refuse.
    """
    if not raw:
        raise UnsafeUpload("That file is empty.")
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        raise UnsafeUpload(f"File is larger than the {settings.max_upload_mb} MB limit.")

    name = (filename or "").lower()
    declared = "." + name.rsplit(".", 1)[-1] if "." in name else ""

    for sig, why in ((PDF_SIG, "PDF"), (RAR_SIG, "RAR archive"), (SEVENZ_SIG, "7-zip archive"),
                     (ELF_SIG, "executable"), (PNG_SIG, "image"), (GZIP_SIG, "gzip archive")):
        if raw.startswith(sig):
            raise UnsafeUpload(f"{why} files are not supported — upload xlsx, csv, tsv, json or docx.")

    if raw.startswith(ZIP_SIG):
        kind = _inspect_zip(raw)
        if kind == "xlsx":
            return ".xlsm" if declared == ".xlsm" else ".xlsx"
        if kind == "docx":
            return ".docx"
        raise UnsafeUpload("That zip archive is not an xlsx or docx workbook.")

    if raw.startswith(OLE_SIG):
        if b"EncryptedPackage" in raw[:8192] or b"E\x00n\x00c\x00r\x00y\x00p\x00t" in raw[:8192]:
            raise UnsafeUpload("This workbook is password protected — remove the password and retry.")
        return ".xls"

    # text-ish payloads
    head = raw[:65536]
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = head.decode("latin-1")
        except Exception:
            raise UnsafeUpload("Unrecognised binary file — upload xlsx, csv, tsv, json or docx.")
    if b"\x00" in head:
        raise UnsafeUpload("Unrecognised binary file — upload xlsx, csv, tsv, json or docx.")
    stripped = text.lstrip()
    if stripped[:1] in "[{":
        return ".json"
    if declared in {".tsv", ".txt"} or ("\t" in text.split("\n")[0] and "," not in text.split("\n")[0]):
        return ".tsv"
    if declared in ALLOWED_EXT and declared in {".csv", ".json", ".txt", ".tsv"}:
        return declared
    return ".csv"


def _inspect_zip(raw: bytes) -> str:
    """Zip-bomb guard + OOXML classification. Raises UnsafeUpload on abuse."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise UnsafeUpload("That file looks like a corrupt zip/xlsx archive.") from exc
    infos = zf.infolist()
    if len(infos) > settings.max_zip_members:
        raise UnsafeUpload(f"Archive has {len(infos)} members — refusing more than "
                           f"{settings.max_zip_members} (possible zip bomb).")
    total = sum(i.file_size for i in infos)
    if total > settings.max_uncompressed_mb * 1024 * 1024:
        raise UnsafeUpload(f"Archive expands to {total // (1024 * 1024)} MB — refusing more than "
                           f"{settings.max_uncompressed_mb} MB (possible zip bomb).")
    compressed = sum(i.compress_size for i in infos) or 1
    if total / compressed > 500 and total > 32 * 1024 * 1024:
        raise UnsafeUpload("Archive compression ratio looks like a zip bomb — rejected.")
    names = zf.namelist()
    if any(n.startswith("/") or ".." in n for n in names):
        raise UnsafeUpload("Archive contains unsafe member paths — rejected.")
    if any(n.startswith("xl/") for n in names):
        if any("EncryptedPackage" in n for n in names):
            raise UnsafeUpload("This workbook is encrypted — remove protection and retry.")
        return "xlsx"
    if any(n.startswith("word/") for n in names):
        return "docx"
    return "other"


def is_formula(value: str) -> bool:
    return bool(_FORMULA_RE.search(value) or _DDE_RE.search(value))


def strip_formulas(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Neutralise formula/DDE payloads in string cells. Returns (df, n_neutralised)."""
    count = 0
    out = df
    for col in df.columns:
        s = df[col]
        if not (pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)):
            continue
        mask = s.map(lambda v: isinstance(v, str) and is_formula(v))
        n = int(mask.sum()) if len(mask) else 0
        if n:
            if out is df:
                out = df.copy()
            out.loc[mask, col] = s[mask].map(lambda v: "'" + v.lstrip())
            count += n
    return out, count


def cap_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Apply row/column caps; returns notes describing any truncation."""
    notes: list[str] = []
    if len(df.columns) > settings.max_cols:
        notes.append(f"kept the first {settings.max_cols} of {len(df.columns)} columns")
        df = df.iloc[:, : settings.max_cols]
    if len(df) > settings.max_rows:
        notes.append(f"kept the first {settings.max_rows:,} of {len(df):,} rows")
        df = df.iloc[: settings.max_rows]
    return df, notes


def sanitize(frames: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], list[str]]:
    out: dict[str, pd.DataFrame] = {}
    notes: list[str] = []
    for sheet, df in frames.items():
        df, capped = cap_frame(df)
        df, n = strip_formulas(df)
        if n:
            notes.append(f"{sheet}: neutralised {n} formula/DDE cell(s)")
        notes += [f"{sheet}: {c}" for c in capped]
        out[sheet] = df
    return out, notes
