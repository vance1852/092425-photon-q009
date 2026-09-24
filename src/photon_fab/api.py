"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import PhotonService


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "photon-fab"})
        with self.service.lock:
            try:
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                parts = self.path.strip("/").split("/")
                if self.path.startswith("/lots/") and len(parts) == 2:
                    return self._json(200, self.service.get_lot(token, parts[1]))
                if self.path.startswith("/lots/") and len(parts) == 3 and parts[2] == "defects":
                    return self._json(200, {"defects": self.service.list_defects(token, parts[1])})
                if self.path.startswith("/defects/"):
                    defect_id = parts[1]
                    if len(parts) == 2:
                        return self._json(200, self.service.get_defect(token, defect_id))
                    if len(parts) == 3 and parts[2] == "history":
                        return self._json(200, {"events": self.service.defect_history(token, defect_id)})
                return self._json(404, {"error": "not found"})
            except PermissionError as exc:
                return self._json(403, {"error": str(exc)})
            except Exception as exc:
                return self._json(400, {"error": str(exc)})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except Exception as exc:
            return self._json(400, {"error": str(exc)})
        with self.service.lock:
            try:
                if self.path == "/login":
                    return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                parts = self.path.strip("/").split("/")
                if self.path == "/lots":
                    return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"]))
                if self.path.startswith("/lots/") and self.path.endswith("/measurements") and len(parts) == 3:
                    lot_id = parts[1]
                    return self._json(201, self.service.add_measurement(token, lot_id, body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"]))
                if self.path.startswith("/lots/") and self.path.endswith("/analysis") and len(parts) == 3:
                    return self._json(200, self.service.analyze(token, parts[1]))
                if self.path.startswith("/lots/") and self.path.endswith("/approvals") and len(parts) == 3:
                    return self._json(200, self.service.approve(token, parts[1], body["decision"], body["reason"]))
                if self.path.startswith("/lots/") and self.path.endswith("/defects") and len(parts) == 3:
                    return self._json(201, self.service.register_defect(
                        token, parts[1], body["category"], body["title"], body["severity"],
                        body.get("description", ""), body["owner"], body["reason"]))
                if self.path.startswith("/defects/"):
                    defect_id = parts[1]
                    if len(parts) == 3 and parts[2] == "rework-tasks":
                        return self._json(201, self.service.create_rework_task(
                            token, defect_id, body["instruction"], body["assignee"], body["reason"]))
                    if len(parts) == 3 and parts[2] == "retests":
                        return self._json(201, self.service.record_retest(
                            token, defect_id, body["result"], body["reason"], body.get("measurement_id")))
                    if len(parts) == 3 and parts[2] == "close":
                        return self._json(200, self.service.close_minor_defect(token, defect_id, body["reason"]))
                if self.path.startswith("/rework-tasks/") and len(parts) == 3 and parts[2] == "complete":
                    return self._json(200, self.service.complete_rework_task(token, parts[1], body["note"]))
                return self._json(404, {"error": "not found"})
            except PermissionError as exc:
                return self._json(403, {"error": str(exc)})
            except Exception as exc:
                return self._json(400, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = PhotonService(args.database)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
