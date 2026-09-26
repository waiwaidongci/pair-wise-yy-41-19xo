import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import STATES, TRANSITION_ROLES
from src.service import Service


def iso(dt):
    return dt.replace(microsecond=0).isoformat()


def future(days):
    return iso(datetime.now(timezone.utc) + timedelta(days=days))


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.point = "P-001"
        self.device = "SN-A"

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _register(self, device="SN-A", factor=1.0, until_days=30,
                  calibrated_at=None, replace=False, point="P-001",
                  valid_from=None):
        payload = {
            "point_code": point, "device_id": device,
            "calibrated_at": calibrated_at or future(0),
            "valid_from": valid_from or future(0),
            "valid_until": future(until_days),
            "correction_factor": factor, "replace": replace,
        }
        return self.service.register_calibration(payload, "cal", "sensor_operator")

    def _create_item(self, raw=10.0, threshold=6.0, device="SN-A", point="P-001"):
        return self.service.create_item({
            "title": "g-1", "description": "gated item", "severity": "warning",
            "quantity": raw, "threshold": threshold,
            "point_code": point, "device_id": device,
            "external_ref": f"X-{point}-{device}-{raw}",
        }, "creator", "sensor_operator")

    def test_missing_calibration_blocks_create(self):
        with self.assertRaises(ValidationError):
            self._create_item()

    def test_expired_calibration_blocks_create(self):
        self._register(until_days=-1, calibrated_at=future(-10),
                       valid_from=future(-10))
        with self.assertRaises(ValidationError) as ctx:
            self._create_item()
        self.assertIn("已过有效期", str(ctx.exception))

    def test_device_mismatch_blocks_create(self):
        self._register(device="SN-A")
        with self.assertRaises(ValidationError) as ctx:
            self._create_item(device="SN-B")
        self.assertIn("设备号对不上", str(ctx.exception))

    def test_valid_calibration_applies_factor(self):
        self._register(factor=1.5)
        item = self._create_item(raw=10.0)
        self.assertEqual(item["raw_quantity"], 10.0)
        self.assertAlmostEqual(item["quantity"], 15.0)
        self.assertEqual(item["calibration"]["effective_calibration_id"],
                         item["calibration_id"])
        self.assertEqual(item["calibration"]["blockers_restriction"], [])

    def test_overlapping_registration_conflicts_and_replace(self):
        first = self._register(factor=1.0, until_days=10)
        with self.assertRaises(ConflictError) as ctx:
            self._register(factor=1.2, until_days=20)
        self.assertIn(str(first["id"]), str(ctx.exception))
        second = self._register(factor=1.2, until_days=20, replace=True)
        self.assertTrue(second["effective_now"])
        old = self.service.get_calibration(first["id"], "viewer")
        self.assertEqual(old["state"], "superseded")
        self.assertFalse(old["effective_now"])
        ledger = self.service.list_calibrations("viewer", self.point)
        self.assertEqual(len(ledger), 2)

    def test_conflict_blocks_transition_until_resolved(self):
        first = self._register(factor=1.0, until_days=10)
        item = self._create_item()
        self._register(factor=1.2, until_days=20, replace=True)
        # 直接落库第二份同时生效的记录，模拟台账重复生效冲突
        self.repo.conn.execute(
            """INSERT INTO calibrations(point_code, device_id, calibrated_at, valid_from,
               valid_until, correction_factor, state, note, created_by, created_at)
               VALUES(?,?,?,?,?,?, 'active', '', ?, ?)""",
            (self.point, "SN-A", future(0), future(0), future(15), 1.3,
             "cal", future(0)))
        self.repo.conn.commit()
        view = self.service.get_item(item["id"], "viewer")
        self.assertEqual(view["calibration"]["state"], "conflict")
        current = self.service.transition(
            item["id"], STATES[1], item["version"], "op",
            TRANSITION_ROLES[STATES[1]][0])
        with self.assertRaises(ConflictError):
            self.service.transition(
                current["id"], STATES[2], current["version"], "eng",
                TRANSITION_ROLES[STATES[2]][0])
        # 解决冲突后，还须按新生效版本重算才放行
        self.repo.conn.execute(
            "UPDATE calibrations SET state='superseded' WHERE valid_until=?",
            (future(15),))
        self.repo.conn.commit()
        self.service.recalibrate({"point_code": self.point}, "eng",
                                 "bridge_engineer")
        current = self.service.get_item(item["id"], "viewer")
        moved = self.service.transition(
            current["id"], STATES[2], current["version"], "eng",
            TRANSITION_ROLES[STATES[2]][0])
        self.assertEqual(moved["status"], "restricted")

    def test_stale_version_blocks_restriction_before_recalculation(self):
        first = self._register(factor=1.0, until_days=10)
        item = self._create_item(raw=10.0)
        self.assertEqual(item["calibration_id"], first["id"])
        self._register(factor=2.0, until_days=30, replace=True)
        view = self.service.get_item(item["id"], "viewer")
        self.assertTrue(view["calibration"]["version_stale"])
        self.assertTrue(any("重算" in b for b in
                            view["calibration"]["blockers_restriction"]))
        current = self.service.transition(
            item["id"], STATES[1], item["version"], "op",
            TRANSITION_ROLES[STATES[1]][0])
        with self.assertRaises(ValidationError):
            self.service.transition(
                current["id"], STATES[2], current["version"], "eng",
                TRANSITION_ROLES[STATES[2]][0])

    def test_recalculate_uses_new_factor_and_preserves_history(self):
        first = self._register(factor=1.0, until_days=10)
        item = self._create_item(raw=10.0)
        self.assertAlmostEqual(item["quantity"], 10.0)
        second = self._register(factor=2.0, until_days=30, replace=True)
        result = self.service.recalibrate(
            {"point_code": self.point}, "eng", "bridge_engineer")
        self.assertEqual(len(result["recalculated"]), 1)
        row = result["recalculated"][0]
        self.assertEqual(row["old_calibration_id"], first["id"])
        self.assertEqual(row["calibration_id"], second["id"])
        self.assertAlmostEqual(row["new_quantity"], 20.0)
        updated = self.service.get_item(item["id"], "viewer")
        self.assertAlmostEqual(updated["quantity"], 20.0)
        self.assertEqual(updated["raw_quantity"], 10.0)
        self.assertEqual(updated["calibration_id"], second["id"])
        self.assertEqual(updated["device_id"], "SN-A")
        self.assertFalse(updated["calibration"]["version_stale"])
        # 历史结论保留：旧校准记录与审计痕迹均在
        self.assertEqual(self.service.get_calibration(first["id"], "viewer")["state"],
                         "superseded")
        events = self.service.audit("viewer", item["id"])
        actions = [e["action"] for e in events]
        self.assertIn("recalibrate", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_recalculate_skips_terminated_and_updates_device(self):
        self._register(factor=1.0, until_days=10)
        item = self._create_item(raw=10.0)
        self.service.add_record(item["id"], {"kind": "traffic",
                                             "detail": "notice", "status": "closed"},
                                "r", "sensor_operator")
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "rv",
                TRANSITION_ROLES[target][0])
        # 另一则未结束告警，仍挂在旧设备上
        item2 = self.service.create_item({
            "title": "g-2", "description": "second", "severity": "warning",
            "quantity": 4.0, "threshold": 6.0, "point_code": self.point,
            "device_id": "SN-A", "external_ref": "X-2"},
            "creator", "sensor_operator")
        # 换传感器后补录新设备校准
        self._register(device="SN-B", factor=3.0, until_days=30, replace=True)
        # 此时未重算前，用旧设备号走限行会被设备号闸门拦下
        stale = self.service.get_item(item2["id"], "viewer")
        self.assertTrue(stale["calibration"]["device_mismatch"])
        result = self.service.recalibrate(
            {"point_code": self.point}, "eng", "bridge_engineer")
        # 已结束（restored）告警不重算；未结束告警按新设备新系数重算
        self.assertEqual(len(result["recalculated"]), 1)
        row = result["recalculated"][0]
        self.assertEqual(row["item_id"], item2["id"])
        self.assertEqual(row["device_id"], "SN-B")
        self.assertAlmostEqual(row["new_quantity"], 12.0)
        updated = self.service.get_item(item2["id"], "viewer")
        self.assertEqual(updated["device_id"], "SN-B")
        self.assertEqual(updated["calibration"]["blockers_restriction"], [])
        # 历史结论保留：已恢复告警仍是恢复态，数值未变
        restored = self.service.get_item(item["id"], "viewer")
        self.assertEqual(restored["status"], "restored")
        self.assertAlmostEqual(restored["quantity"], 10.0)

    def test_register_permissions(self):
        payload = {"point_code": "P", "device_id": "D", "valid_until": future(5),
                   "correction_factor": 1.0}
        with self.assertRaises(PermissionDenied):
            self.service.register_calibration(payload, "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.recalibrate({}, "x", "sensor_operator")

    def test_validation_rules(self):
        with self.assertRaises(ValidationError):
            self.service.register_calibration(
                {"point_code": "P", "device_id": "D", "valid_until": future(1),
                 "correction_factor": 0}, "c", "sensor_operator")
        with self.assertRaises(ValidationError):
            self.service.register_calibration(
                {"point_code": "P", "device_id": "D",
                 "valid_from": future(10), "valid_until": future(5),
                 "correction_factor": 1}, "c", "sensor_operator")


if __name__ == "__main__":
    unittest.main()
