"""Local office UI and read-only Codex job runner."""
import json
import os
import subprocess
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

OFFICE = Path(__file__).resolve().parent
PROJECT = OFFICE.parent
ROLES = {
    path.stem: tomllib.loads(path.read_text())
    for path in sorted((PROJECT / ".codex/agents").glob("*.toml"))
}
STATE = Path(os.environ.get("AGENCY_OFFICE_STATE", OFFICE / "data/jobs.json"))
jobs = []
lock = Lock()
worker = ThreadPoolExecutor(max_workers=1)


def save():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    pending = STATE.with_suffix(".tmp")
    pending.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    pending.replace(STATE)


def update(job, **fields):
    with lock:
        job.update(fields)
        save()


def run(job):
    role = ROLES[job["role"]]
    prompt = (
        f"ทำงานในฐานะ {role['description']}\n"
        f"{role['developer_instructions']}\n"
        "สำหรับต้นแบบนี้ ให้ตอบเป็นข้อความในแชทเท่านั้น "
        "อย่าเรียกเอเจนต์อื่น แก้ไฟล์ หรือส่งข้อมูลออกไป\n"
        f"โจทย์จากผู้ใช้:\n{job['prompt']}"
    )
    try:
        with subprocess.Popen(
            ["codex", "--ask-for-approval", "never", "exec", "--ephemeral", "--skip-git-repo-check",
             "--sandbox", "read-only", "--json", "-"],
            cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, encoding="utf-8",
        ) as process:
            update(job, status="running")
            try:
                output, _ = process.communicate(prompt, timeout=180)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            if process.returncode:
                raise RuntimeError("Codex ทำงานไม่สำเร็จ ตรวจการเข้าสู่ระบบและการเชื่อมต่อ แล้วลองส่งใหม่")
        messages = []
        for line in output.splitlines():
            event = json.loads(line)
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                messages.append(item["text"])
        if not messages:
            raise RuntimeError("Codex ไม่ส่งคำตอบกลับมา")
        answer = "\n\n".join(messages)
        with lock:
            job.update(status="done", answer=answer)
            child = None
            if job.get("handoff"):
                child = {"id": uuid.uuid4().hex, "role": "graphics",
                         "prompt": f"ทำร่างงานกราฟิกจากบรีฟนี้\n{answer}",
                         "status": "queued", "answer": "", "parent_id": job["id"]}
                jobs.append(child)
            save()
            if child:
                worker.submit(run, child)
    except subprocess.TimeoutExpired:
        update(job, status="failed", answer="Codex ใช้เวลานานเกิน 3 นาที ลองส่งงานอีกครั้ง")
    except (OSError, RuntimeError, ValueError) as error:
        message = str(error) if isinstance(error, RuntimeError) else "อ่านผลจาก Codex ไม่สำเร็จ"
        update(job, status="failed", answer=message)


class OfficeHandler(BaseHTTPRequestHandler):
    def respond(self, status, data, mime="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.respond(200, (OFFICE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/bangkok-day.webp":
            self.respond(200, (OFFICE / "bangkok-day.webp").read_bytes(), "image/webp")
        elif self.path == "/bangkok-night.webp":
            self.respond(200, (OFFICE / "bangkok-night.webp").read_bytes(), "image/webp")
        elif self.path == "/api/jobs":
            with lock:
                snapshot = [job.copy() for job in jobs]
            self.respond(200, snapshot)
        else:
            self.respond(404, {"error": "ไม่พบหน้า"})

    def do_POST(self):
        if self.path != "/api/jobs":
            self.respond(404, {"error": "ไม่พบหน้า"})
            return
        # Require a same-origin JSON request to the loopback service.
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in {"127.0.0.1:8765", "localhost:8765"} or (
            origin and origin != "http://" + host
        ) or (
            self.headers.get_content_type() != "application/json"
        ):
            self.respond(403, {"error": "ส่งงานจากหน้าออฟฟิศเท่านั้น"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 24000:
                raise ValueError
            body = json.loads(self.rfile.read(size))
            role = body["role"]
            prompt = body["prompt"].strip()
            if role not in ROLES or not 0 < len(prompt) <= 4000:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            self.respond(400, {"error": "ตรวจตำแหน่งและข้อความก่อนส่ง"})
            return
        with lock:
            job = {"id": uuid.uuid4().hex, "role": role, "prompt": prompt,
                   "status": "queued", "answer": "",
                   "handoff": role == "content" and body.get("handoff") is True}
            jobs.append(job)
            save()
            worker.submit(run, job)
        self.respond(202, {"id": job["id"]})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), OfficeHandler)
    if STATE.exists():
        jobs.extend(json.loads(STATE.read_text(encoding="utf-8")))
        for job in jobs:
            if job["status"] in {"queued", "running"}:
                job.update(status="failed", answer="ตัวรันถูกหยุดก่อนงานเสร็จ ส่งงานใหม่เพื่อเริ่มอีกครั้ง")
        save()
    print("AI Agency Office · http://127.0.0.1:8765", flush=True)
    server.serve_forever()
