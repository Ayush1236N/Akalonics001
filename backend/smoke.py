"""Quick end-to-end smoke over ASGI (no server needed). python smoke.py"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("DATA_DIR", "/tmp/ds-smoke")

import httpx  # noqa: E402


async def main() -> None:
    from app import app
    from db import init_models
    await init_models()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        print("health", (await c.get("/api/health")).json())
        print("ready", (await c.get("/api/ready")).json())
        r = await c.post("/api/auth/guest")
        tok = r.json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        print("guest", r.json()["user"])
        print("no-auth rows ->", (await c.get("/api/rows/x")).status_code)
        s = await c.post("/api/sample/sales.xlsx", headers=h)
        print("sample", s.status_code, [x["name"] for x in s.json()["sheets"]])
        dsid = s.json()["dataset_id"]
        a = await c.post("/api/analyze", json={"dataset_id": dsid}, headers=h)
        print("analyze", a.status_code, len(a.json()["slides"]), a.json()["slides"][0]["type"])
        for fmt in ("pptx", "xlsx", "pdf"):
            e = await c.get(f"/api/export/{dsid}?format={fmt}", headers=h)
            print("export", fmt, e.status_code, len(e.content))
        sid = a.json()["slides"][0]["id"]
        p = await c.get(f"/api/export/png/{dsid}/{sid}", headers=h)
        print("png", p.status_code, p.content[:4])
        nl = await c.post("/api/nl-chart", json={"dataset_id": dsid,
                                                 "prompt": "show revenue by region as a treemap"},
                          headers=h)
        print("nl", nl.status_code, nl.json().get("source"), nl.json().get("slide", {}).get("type"))
        u = await c.post("/api/upload", files={"file": ("survey.csv",
                                                       open("../samples/survey.csv", "rb"),
                                                       "text/csv")}, headers=h)
        print("upload", u.status_code, u.json())
        jid = u.json()["job_id"]
        for _ in range(120):
            j = (await c.get(f"/api/jobs/{jid}", headers=h)).json()
            if j["state"] in ("done", "failed"):
                break
            await asyncio.sleep(0.25)
        print("job", j["state"], j["detail"])


asyncio.run(main())
