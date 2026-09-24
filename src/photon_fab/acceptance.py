"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import PhotonService


def run() -> dict:
    service = PhotonService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "photon-admin")
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(token, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    result = service.analyze(token, "LOT-DEMO")
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")

    # 封装测试发现暗电流超限（严重缺陷）：批次保持 hold，返工 + 复测通过
    # 并由质量关闭后才允许重新审批放行。
    defect = service.defects.register_defect(
        token, "LOT-DEMO", "DC-01", "暗电流超限", "critical",
        "packaging-test", "admin", "暗电流 38nA 超过 10nA 限值")
    task = service.defects.dispatch_rework(
        token, defect["defect_id"], "admin", "清洗钝化并重新镀膜", "返工工艺 RW-1")
    service.defects.complete_rework(
        token, task["rework_tasks"][-1]["task_id"], "重新镀膜完成", "返工完成")
    service.defects.record_retest(
        token, defect["defect_id"], "pass", {"dark_current_na": 4.2},
        "暗电流合格", "复测通过")
    service.defects.close_defect(token, defect["defect_id"], "复测通过，质量确认")
    lot = service.approve(token, "LOT-DEMO", "release", "缺陷闭环，批准放行")

    return {"status": "ok", "lot": result["lot_id"],
            "peak": result["spectrum"]["peak_wavelength_nm"],
            "lot_status": lot["status"],
            "defect_status": "closed",
            "events": len(service.audit(token, "LOT-DEMO"))}


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
