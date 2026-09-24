from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from photon_fab.api import Handler
from photon_fab.service import PhotonService


class HttpServer:
    def __init__(self) -> None:
        Handler.service = PhotonService(":memory:")
        Handler.service.bootstrap_admin()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class DefectApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd = HttpServer()
        svc = Handler.service
        for user_id, role, password in (
            ("op", "operator", "operator1"),
            ("eng", "engineer", "engineer1"),
            ("qa", "quality", "quality-12"),
        ):
            svc.auth.create_user(user_id, password, role)
        self.tokens = {
            "op": svc.auth.login("op", "operator1"),
            "eng": svc.auth.login("eng", "engineer1"),
            "qa": svc.auth.login("qa", "quality-12"),
        }

    def tearDown(self) -> None:
        self.httpd.stop()

    def _request(self, method: str, path: str, token: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.port)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def test_defect_gate_workflow_over_http(self) -> None:
        self._request("POST", "/lots", self.tokens["eng"],
                      {"lot_id": "LOT-9", "product": "PD", "process_rev": "P1", "wafer_count": 4})

        status, defect = self._request("POST", "/lots/LOT-9/defects", self.tokens["op"], {
            "code": "DC-01", "title": "暗电流超限", "severity": "critical",
            "owner": "eng", "reason": "38nA > 10nA"})
        self.assertEqual(status, 201)
        defect_id = defect["defect_id"]

        # 严重缺陷未闭环：HTTP 放行动作返回 409
        status, body = self._request("POST", "/lots/LOT-9/approvals", self.tokens["qa"],
                                     {"decision": "release", "reason": "尝试放行"})
        self.assertEqual(status, 409)
        self.assertIn("blocked", body["error"])

        status, task = self._request("POST", f"/defects/{defect_id}/rework", self.tokens["eng"],
                                     {"assignee": "op", "instruction": "重新镀膜", "reason": "RW-1"})
        self.assertEqual(status, 201)
        task_id = task["rework_tasks"][-1]["task_id"]

        # 返工完成前再次尝试放行仍 409
        status, _ = self._request("POST", "/lots/LOT-9/approvals", self.tokens["qa"],
                                  {"decision": "release", "reason": "返工中放行"})
        self.assertEqual(status, 409)

        status, _ = self._request("POST", f"/rework/{task_id}/complete", self.tokens["op"],
                                  {"note": "完工", "reason": "返工完成"})
        self.assertEqual(status, 200)

        status, _ = self._request("POST", f"/defects/{defect_id}/retests", self.tokens["op"],
                                  {"result": "pass", "data": {"dark_current_na": 4.2},
                                   "note": "合格", "reason": "复测通过"})
        self.assertEqual(status, 201)

        # 复测通过但未关闭仍 409
        status, _ = self._request("POST", "/lots/LOT-9/approvals", self.tokens["qa"],
                                  {"decision": "release", "reason": "未关闭放行"})
        self.assertEqual(status, 409)

        status, closed = self._request("POST", f"/defects/{defect_id}/close",
                                       self.tokens["qa"], {"reason": "质量确认闭环"})
        self.assertEqual(status, 200)
        self.assertEqual(closed["status"], "closed")

        # 闭环后质量人员重新审批放行
        status, lot = self._request("POST", "/lots/LOT-9/approvals", self.tokens["qa"],
                                    {"decision": "release", "reason": "缺陷闭环放行"})
        self.assertEqual(status, 200)
        self.assertEqual(lot["status"], "released")

        status, history = self._request("GET", f"/defects/{defect_id}/history",
                                        self.tokens["qa"])
        self.assertEqual(status, 200)
        self.assertEqual([e["event_type"] for e in history["events"]][0], "registered")
        self.assertTrue(all(e["reason"] and e["actor"] for e in history["events"]))

    def test_operator_cannot_dispatch_rework(self) -> None:
        self._request("POST", "/lots", self.tokens["eng"],
                      {"lot_id": "LOT-8", "product": "PD", "process_rev": "P1", "wafer_count": 4})
        _, defect = self._request("POST", "/lots/LOT-8/defects", self.tokens["op"], {
            "code": "SCR", "title": "划痕", "severity": "major",
            "owner": "op", "reason": "目检"})
        status, body = self._request("POST", f"/defects/{defect['defect_id']}/rework",
                                     self.tokens["op"],
                                     {"assignee": "op", "instruction": "抛光", "reason": "x"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
