"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import PhotonService


def run() -> dict:
    service = PhotonService()
    service.bootstrap_admin()
    admin = service.auth.login("admin", "photon-admin")
    service.auth.create_user("quality-1", "quality-pass", "quality")
    service.auth.create_user("operator-1", "operator-pass", "operator")
    quality = service.auth.login("quality-1", "quality-pass")
    operator = service.auth.login("operator-1", "operator-pass")
    service.create_lot(admin, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for wavelength, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(operator, "LOT-DEMO", wavelength, response, .01, "spectrometer-1")
    result = service.analyze(quality, "LOT-DEMO")

    # 封装测试发现暗电流超限（严重缺陷）：登记后批次立即 hold，放行被阻止
    defect = service.register_defect(
        quality, "LOT-DEMO", "dark_current", "暗电流超限", "critical",
        "暗电流 12nA，超过规格上限 8nA", owner="engineer-1",
        reason="封装测试工位 QC-2 发现暗电流超限")
    blocked = None
    try:
        service.approve(quality, "LOT-DEMO", "release", "尝试放行")
    except PermissionError as exc:
        blocked = str(exc)

    # 返工：指派责任人、完成返工、复测通过
    task = service.create_rework_task(
        operator, defect["defect_id"], "重新键合并更换偏置电阻", "engineer-1",
        reason="工程评审决定返工键合工艺")
    service.complete_rework_task(operator, task["task_id"], "已完成重新键合，暗电流待复测")
    retest = service.record_retest(
        quality, defect["defect_id"], "pass", "复测暗电流 6.2nA，符合规格")
    service.approve(quality, "LOT-DEMO", "release", "严重缺陷已关闭且复测通过")

    lot = service.get_lot(quality, "LOT-DEMO")
    history = service.defect_history(quality, defect["defect_id"])
    return {
        "status": "ok", "lot": result["lot_id"],
        "peak": result["spectrum"]["peak_wavelength_nm"],
        "defect_status": service.get_defect(quality, defect["defect_id"])["status"],
        "release_was_blocked": blocked is not None,
        "retest_result": retest["result"],
        "final_lot_status": lot["status"],
        "defect_events": len(history),
        "events": len(service.audit(quality, "LOT-DEMO")),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
