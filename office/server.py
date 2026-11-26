"""Local office UI and read-only Codex job runner."""
import json
import os
import shutil
import subprocess
import sys
import tomllib
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

OFFICE = Path(__file__).resolve().parent
PROJECT = OFFICE.parent
ROLES = {
    path.stem: tomllib.loads(path.read_text(encoding="utf-8"))
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


def codex_text(prompt):
    executable = os.environ.get("AGENCY_CODEX_EXE") or shutil.which("codex")
    if not executable or not Path(executable).is_file():
        raise RuntimeError("หาไฟล์ Codex CLI ไม่เจอ ตั้ง AGENCY_CODEX_EXE ให้ชี้ไปที่ codex.exe แล้วเปิดออฟฟิศใหม่")
    with subprocess.Popen(
        [executable, "--ask-for-approval", "never", "exec", "--ephemeral", "--skip-git-repo-check",
         "--sandbox", "read-only", "--json", "-"],
        cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
    ) as process:
        try:
            output, diagnostics = process.communicate(prompt, timeout=180)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.communicate()
            raise RuntimeError("Codex ใช้เวลานานเกิน 3 นาที ลองส่งงานอีกครั้ง") from error
        if process.returncode:
            if diagnostics.strip():
                print(diagnostics.strip(), file=sys.stderr, flush=True)
            raise RuntimeError("Codex ทำงานไม่สำเร็จ ดูรายละเอียดในหน้าต่างที่เปิดออฟฟิศ")
    messages = []
    for line in output.splitlines():
        event = json.loads(line)
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            messages.append(item["text"])
    if not messages:
        raise RuntimeError("Codex ไม่ส่งคำตอบกลับมา")
    return messages[-1]


def role_prompt(role, prompt):
    config = ROLES[role]
    return (
        f"ทำงานในฐานะ {config['description']}\n"
        f"{config['developer_instructions']}\n"
        "ตอบเป็นร่างข้อความในแชทเท่านั้น แยกสิ่งที่ทำได้จากสิ่งที่ต้องใช้ไฟล์หรือเครื่องมือเพิ่ม "
        "อย่าเรียกเอเจนต์อื่น แก้ไฟล์ เผยแพร่ หรือติดต่อบุคคลภายนอก\n"
        f"{prompt}"
    )


def make_job(role, prompt, **metadata):
    return {"id": uuid.uuid4().hex, "role": role, "prompt": prompt,
            "status": "queued", "answer": "", **metadata}


def run(job, context=None):
    update(job, status="running")
    try:
        answer = codex_text(role_prompt(job["role"], context or f"โจทย์จากผู้ใช้:\n{job['prompt']}"))
    except (OSError, RuntimeError, ValueError) as error:
        message = str(error) if isinstance(error, RuntimeError) else "อ่านผลจาก Codex ไม่สำเร็จ"
        update(job, status="failed", answer=message)
        return
    with lock:
        job.update(status="done", answer=answer)
        child = None
        if job.get("handoff"):
            child = make_job("graphics", f"ทำร่างงานกราฟิกจากบรีฟนี้\n{answer}", parent_id=job["id"])
            jobs.append(child)
        save()
    if child:
        worker.submit(run, child)


def plan_team(brief):
    roster = "\n".join(
        f"{role}: {config['description']} — {config['developer_instructions'][:1000]}"
        for role, config in ROLES.items()
    )
    response = codex_text(
        "คุณคือผู้ประสานงานเอเจนซี่ วางแผนงานร่างข้อความเท่านั้น อย่าใช้เครื่องมือหรือแก้ไฟล์ "
        "เลือกเฉพาะตำแหน่งที่จำเป็นจากรายชื่อ ให้มีตำแหน่งละไม่เกินหนึ่งงาน "
        "ระบุชิ้นงานที่ต้องรอจากตำแหน่งก่อนหน้า ถ้าไม่มีให้ใช้ []. "
        'ตอบ JSON เท่านั้นในรูป {"steps":[{"role":"content","task":"ร่างบรีฟ","needs":[]}]}.\n'
        f"รายชื่อ:\n{roster}\nโจทย์:\n{brief}"
    ).strip()
    try:
        if response.startswith("```") and response.endswith("```"):
            response = response.split("\n", 1)[1].rsplit("```", 1)[0]
        steps = json.loads(response)["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= len(ROLES):
            raise ValueError
        assigned = set()
        for step in steps:
            role, task, needs = step["role"], step["task"], step["needs"]
            if (role not in ROLES or role in assigned or not isinstance(task, str)
                    or not task.strip() or len(task) > 1200 or not isinstance(needs, list)
                    or not set(needs) <= assigned or len(needs) != len(set(needs))):
                raise ValueError
            assigned.add(role)
        return steps
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError("แผนที่ Codex ส่งกลับมาใช้จัดคิวไม่ได้ ลองส่งโจทย์อีกครั้ง") from error


def run_team(job):
    update(job, status="running", answer="กำลังวางแผนและแบ่งงาน")
    try:
        steps = plan_team(job["prompt"])
    except (OSError, RuntimeError, ValueError) as error:
        message = str(error) if isinstance(error, RuntimeError) else "อ่านแผนจาก Codex ไม่สำเร็จ"
        update(job, status="failed", answer=message)
        return
    assigned = {
        step["role"]: make_job(step["role"], step["task"], team_id=job["id"], needs=step["needs"])
        for step in steps
    }
    with lock:
        job["answer"] = "แผนงานพร้อมแล้ว ทีมจะทำตามลำดับงานที่ต้องส่งต่อ"
        jobs.extend(assigned.values())
        save()
    for step in steps:
        child = assigned[step["role"]]
        prerequisites = [assigned[role] for role in step["needs"]]
        if any(item["status"] != "done" for item in prerequisites):
            update(child, status="failed", answer="รอผลงานต้นทางที่ทำไม่สำเร็จ จึงยังเริ่มขั้นนี้ไม่ได้")
            continue
        if prerequisites:
            update(child, parent_id=prerequisites[-1]["id"])
        inputs = "\n\n".join(
            f"{ROLES[item['role']]['description']}:\n{item['answer'][:10000]}"
            for item in prerequisites
        )
        run(child, f"โจทย์รวม:\n{job['prompt']}\n\nงานของคุณ:\n{child['prompt']}\n\n"
                   f"ผลงานจากทีมสำหรับใช้อ้างอิง:\n{inputs or 'ไม่มีงานต้นทาง'}")
    completed = sum(child["status"] == "done" for child in assigned.values())
    update(job, status="done" if completed == len(steps) else "failed",
           answer=f"ทำเสร็จ {completed} จาก {len(steps)} งาน ดูผลและสถานะของแต่ละตำแหน่งด้านล่าง")


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
        elif self.path == "/software-renderer.mjs":
            self.respond(200, (OFFICE / "software-renderer.mjs").read_bytes(), "text/javascript; charset=utf-8")
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
            if (role != "team" and role not in ROLES) or not 0 < len(prompt) <= 4000:
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            self.respond(400, {"error": "ตรวจตำแหน่งและข้อความก่อนส่ง"})
            return
        with lock:
            job = make_job(role, prompt, handoff=role == "content" and body.get("handoff") is True)
            jobs.append(job)
            save()
            worker.submit(run_team if role == "team" else run, job)
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
