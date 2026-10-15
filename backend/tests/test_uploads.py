"""Upload of each file type + malicious-upload rejection."""
from __future__ import annotations

import io
import json
import zipfile

import pytest

import uploads
from conftest import SAMPLES, new_guest

pytestmark = pytest.mark.asyncio


async def _wait(client, headers, job_id, tries=200):
    import asyncio
    for _ in range(tries):
        r = await client.get(f"/api/jobs/{job_id}", headers=headers)
        body = r.json()
        if body["state"] in ("done", "failed"):
            return body
        await asyncio.sleep(0.1)
    raise AssertionError("job never finished")


@pytest.mark.parametrize("name,content", [
    ("sales.xlsx", None),
    ("survey.csv", None),
    ("report.docx", None),
    ("data.tsv", b"region\tunits\nnorth\t12\nsouth\t18\neast\t7\nwest\t22\n"),
    ("data.json", json.dumps([{"region": "north", "units": 12},
                              {"region": "south", "units": 18},
                              {"region": "east", "units": 7}]).encode()),
])
async def test_upload_each_file_type(client, name, content):
    g = await new_guest(client)
    raw = content if content is not None else (SAMPLES / name).read_bytes()
    r = await client.post("/api/upload", files={"file": (name, io.BytesIO(raw),
                                                         "application/octet-stream")},
                          headers=g["headers"])
    assert r.status_code == 200, r.text
    job = await _wait(client, g["headers"], r.json()["job_id"])
    assert job["state"] == "done", job
    ds = await client.get(f"/api/datasets/{r.json()['dataset_id']}", headers=g["headers"])
    assert ds.status_code == 200
    assert ds.json()["sheets"], ds.text


async def test_upload_progress_stream_reports_worker_states(client):
    g = await new_guest(client)
    r = await client.post("/api/upload",
                          files={"file": ("survey.csv", (SAMPLES / "survey.csv").open("rb"),
                                          "text/csv")},
                          headers=g["headers"])
    job_id = r.json()["job_id"]
    stages = []
    async with client.stream("GET", f"/api/progress/{job_id}", headers=g["headers"]) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                stages.append(payload["stage"])
                if payload["stage"] in ("done", "error"):
                    break
    assert stages[-1] == "done", stages
    assert set(stages) & {"queued", "parsing", "profiling", "planning"}, stages


# --- malicious / unsafe -----------------------------------------------------
async def test_rejects_pdf_bytes_named_csv(client):
    g = await new_guest(client)
    r = await client.post("/api/upload",
                          files={"file": ("evil.csv", io.BytesIO(b"%PDF-1.7\n junk"), "text/csv")},
                          headers=g["headers"])
    assert r.status_code == 422 and "PDF" in r.json()["detail"]


async def test_rejects_executable(client):
    g = await new_guest(client)
    r = await client.post("/api/upload",
                          files={"file": ("x.csv", io.BytesIO(b"\x7fELF\x02\x01\x01" + b"\x00" * 64),
                                          "text/csv")},
                          headers=g["headers"])
    assert r.status_code == 422


def test_zip_bomb_member_count_guard():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "<x/>")
        for i in range(2100):
            zf.writestr(f"xl/f{i}.xml", "a")
    with pytest.raises(uploads.UnsafeUpload) as exc:
        uploads.sniff("bomb.xlsx", buf.getvalue())
    assert "zip bomb" in str(exc.value)


def test_zip_bomb_uncompressed_size_guard(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "max_uncompressed_mb", 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "0" * (4 * 1024 * 1024))
    with pytest.raises(uploads.UnsafeUpload):
        uploads.sniff("bomb.xlsx", buf.getvalue())


def test_encrypted_workbook_rejected():
    ole = uploads.OLE_SIG + b"\x00" * 100 + b"EncryptedPackage" + b"\x00" * 100
    with pytest.raises(uploads.UnsafeUpload) as exc:
        uploads.sniff("locked.xls", ole)
    assert "password protected" in str(exc.value)


def test_zip_that_is_not_ooxml_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "hi")
    with pytest.raises(uploads.UnsafeUpload):
        uploads.sniff("notes.xlsx", buf.getvalue())


def test_oversize_rejected(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    with pytest.raises(uploads.UnsafeUpload):
        uploads.sniff("big.csv", b"a," * 900_000)


@pytest.mark.parametrize("value,flagged", [
    ("=1+1", True), ("=cmd|'/c calc'!A1", True), ("@SUM(A1)", True),
    ("+HYPERLINK(\"http://x\")", True), ("-cmd|", True),
    ("-12.5", False), ("+44 7700 900000", False), ("normal text", False),
])
def test_formula_detection(value, flagged):
    assert uploads.is_formula(value) is flagged


def test_formula_stripping_neutralises_cells():
    import pandas as pd
    df = pd.DataFrame({"a": ["=cmd|'/c calc'!A1", "ok"], "n": [1, 2]})
    out, n = uploads.strip_formulas(df)
    assert n == 1
    assert out.loc[0, "a"].startswith("'=")
    assert out.loc[1, "a"] == "ok"


def test_row_and_col_caps(monkeypatch):
    import pandas as pd
    from config import settings
    monkeypatch.setattr(settings, "max_rows", 5)
    monkeypatch.setattr(settings, "max_cols", 2)
    df = pd.DataFrame({f"c{i}": range(20) for i in range(6)})
    out, notes = uploads.cap_frame(df)
    assert out.shape == (5, 2) and len(notes) == 2


def test_sniff_detects_real_types():
    assert uploads.sniff("sales.xlsx", (SAMPLES / "sales.xlsx").read_bytes()) == ".xlsx"
    assert uploads.sniff("report.docx", (SAMPLES / "report.docx").read_bytes()) == ".docx"
    assert uploads.sniff("survey.csv", (SAMPLES / "survey.csv").read_bytes()) == ".csv"
    assert uploads.sniff("x.json", b'[{"a":1}]') == ".json"
    assert uploads.sniff("x.tsv", b"a\tb\n1\t2\n") == ".tsv"
