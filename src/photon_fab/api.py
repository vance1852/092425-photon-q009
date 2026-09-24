"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .defects import QualityGateError
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
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            if self.path.startswith("/defects/"):
                parts = self.path.strip("/").split("/")
                if len(parts) == 2:
                    return self._json(200, self.service.defects.get_defect(token, parts[1]))
                if len(parts) == 3 and parts[2] == "history":
                    return self._json(200, {"events": self.service.defects.history(token, parts[1])})
            if self.path.startswith("/lots/"):
                parts = self.path.strip("/").split("/")
                if len(parts) == 2:
                    return self._json(200, self.service.get_lot(token, parts[1]))
                if len(parts) == 3 and parts[2] == "defects":
                    return self._json(200, {"defects": self.service.defects.list_defects(token, parts[1])})
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except KeyError as exc:
            return self._json(404, {"error": f"not found: {exc.args[0]}"})
        except Exception as exc:
            return self._json(400, {"error": str(exc)})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"]))
            if self.path.startswith("/lots/") and self.path.endswith("/measurements"):
                lot_id = self.path.split("/")[2]
                return self._json(201, self.service.add_measurement(token, lot_id, body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"]))
            if self.path.startswith("/lots/") and self.path.endswith("/analysis"):
                return self._json(200, self.service.analyze(token, self.path.split("/")[2]))
            if self.path.startswith("/lots/") and self.path.endswith("/approvals"):
                lot_id = self.path.split("/")[2]
                return self._json(200, self.service.approve(
                    token, lot_id, body["decision"], body["reason"]))
            if self.path.startswith("/lots/") and self.path.endswith("/defects"):
                lot_id = self.path.split("/")[2]
                return self._json(201, self.service.defects.register_defect(
                    token, lot_id, body["code"], body["title"], body["severity"],
                    body.get("source", "packaging-test"), body["owner"], body["reason"]))
            if self.path.startswith("/defects/"):
                parts = self.path.strip("/").split("/")
                defect_id = parts[1]
                if len(parts) == 3 and parts[2] == "rework":
                    return self._json(201, self.service.defects.dispatch_rework(
                        token, defect_id, body["assignee"], body["instruction"], body["reason"]))
                if len(parts) == 3 and parts[2] == "retests":
                    return self._json(201, self.service.defects.record_retest(
                        token, defect_id, body["result"], body.get("data"),
                        body.get("note", ""), body["reason"]))
                if len(parts) == 3 and parts[2] == "close":
                    return self._json(200, self.service.defects.close_defect(token, defect_id, body["reason"]))
                if len(parts) == 3 and parts[2] == "void":
                    return self._json(200, self.service.defects.void_defect(token, defect_id, body["reason"]))
            if self.path.startswith("/rework/") and self.path.endswith("/complete"):
                task_id = self.path.strip("/").split("/")[1]
                return self._json(200, self.service.defects.complete_rework(
                    token, task_id, body.get("note", ""), body["reason"]))
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except QualityGateError as exc:
            return self._json(409, {"error": str(exc)})
        except KeyError as exc:
            return self._json(404, {"error": f"not found: {exc.args[0]}"})
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
