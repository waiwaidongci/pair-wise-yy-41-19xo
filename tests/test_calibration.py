import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES

FUTURE="2099-01-01T00:00:00Z"; PAST="2020-01-01T00:00:00Z"; CAL_AT="2026-01-01T00:00:00Z"

def cal(point="PT-1",device="DEV-1",factor=1.0,until=FUTURE,replace=False,at=CAL_AT):
    return {"point_ref":point,"device_id":device,"calibrated_at":at,"factor":factor,"valid_until":until,"replace":replace}

def alarm(point="PT-1",device="DEV-1",ref="AL-1",qty=10.0):
    return {"title":"alarm","description":"sensor drift","severity":"warning","quantity":qty,"threshold":5,"external_ref":ref,"point_ref":point,"device_id":device}

class CalibrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()

    def test_register_and_create_applies_factor(self):
        c=self.service.register_calibration(cal(factor=1.5),"op",'sensor_operator')
        item=self.service.create_item(alarm(qty=10.0),"creator",'sensor_operator')
        self.assertEqual(item["calibration_id"],c["id"]); self.assertEqual(item["factor"],1.5)
        self.assertEqual(item["corrected_quantity"],15.0); self.assertEqual(item["calibration_blockers"],[])
        self.assertEqual(item["calibration"]["id"],c["id"])

    def test_missing_expired_and_device_mismatch_block_creation(self):
        with self.assertRaisesRegex(ConflictError,"校准缺失"):
            self.service.create_item(alarm(point="PT-X"),"creator",'sensor_operator')
        self.service.register_calibration(cal(point="PT-OLD",until=PAST,at="2019-01-01T00:00:00Z"),"op",'sensor_operator')
        with self.assertRaisesRegex(ConflictError,"校准过期"):
            self.service.create_item(alarm(point="PT-OLD"),"creator",'sensor_operator')
        self.service.register_calibration(cal(),"op",'sensor_operator')
        with self.assertRaisesRegex(ConflictError,"设备号对不上"):
            self.service.create_item(alarm(device="DEV-2"),"creator",'sensor_operator')

    def test_duplicate_active_conflicts_and_replace_supersedes(self):
        first=self.service.register_calibration(cal(),"op",'sensor_operator')
        with self.assertRaisesRegex(ConflictError,"已存在生效校准"):
            self.service.register_calibration(cal(device="DEV-2"),"op",'sensor_operator')
        second=self.service.register_calibration(cal(device="DEV-2",factor=2.0,replace=True),"op",'sensor_operator')
        ledger=self.service.list_calibrations("viewer","PT-1")
        self.assertEqual(len(ledger),2)
        active=[c for c in ledger if c["status"]=="active"]; superseded=[c for c in ledger if c["status"]=="superseded"]
        self.assertEqual([c["id"] for c in active],[second["id"]])
        self.assertEqual([c["id"] for c in superseded],[first["id"]])

    def test_calibration_validation_and_roles(self):
        with self.assertRaises(PermissionDenied): self.service.register_calibration(cal(),"op",'viewer')
        with self.assertRaises(ValueError): self.service.register_calibration(cal(factor=0),"op",'sensor_operator')
        with self.assertRaises(ValidationError): self.service.register_calibration(cal(until="not-a-time"),"op",'sensor_operator')
        with self.assertRaises(ValueError): self.service.register_calibration(cal(until=CAL_AT),"op",'sensor_operator')

    def test_restrict_close_blocked_until_recalibrated(self):
        self.service.register_calibration(cal(),"op",'sensor_operator')
        item=self.service.create_item(alarm(),"creator",'sensor_operator')
        item=self.service.transition(item["id"],STATES[1],item["version"],"op",TRANSITION_ROLES[STATES[1]][0])
        self.service.register_calibration(cal(device="DEV-2",factor=2.0,replace=True),"op",'sensor_operator')
        with self.assertRaisesRegex(ConflictError,"设备号对不上"):
            self.service.transition(item["id"],STATES[2],item["version"],"eng",TRANSITION_ROLES[STATES[2]][0])
        result=self.service.recalculate_point("PT-1",{"device_id":"DEV-2"},"op",'sensor_operator')
        self.assertEqual(result["updated"],[item["id"]]); self.assertEqual(result["blocked"],[])
        item=self.service.get_item(item["id"],"viewer")
        self.assertEqual(item["factor"],2.0); self.assertEqual(item["corrected_quantity"],20.0)
        item=self.service.transition(item["id"],STATES[2],item["version"],"eng",TRANSITION_ROLES[STATES[2]][0])
        self.assertEqual(item["status"],STATES[2])

    def test_expired_calibration_blocks_restrict(self):
        self.service.register_calibration(cal(),"op",'sensor_operator')
        item=self.service.create_item(alarm(),"creator",'sensor_operator')
        item=self.service.transition(item["id"],STATES[1],item["version"],"op",TRANSITION_ROLES[STATES[1]][0])
        self.service.register_calibration(cal(until=PAST,at="2019-01-01T00:00:00Z",replace=True),"op",'sensor_operator')
        with self.assertRaisesRegex(ConflictError,"校准过期"):
            self.service.transition(item["id"],STATES[2],item["version"],"eng",TRANSITION_ROLES[STATES[2]][0])

    def test_recalculate_skips_terminal_and_reports_blocked(self):
        self.service.register_calibration(cal(),"op",'sensor_operator')
        done=self.service.create_item(alarm(ref="AL-DONE"),"creator",'sensor_operator')
        current=done
        for target in STATES[1:]:
            current=self.service.transition(current["id"],target,current["version"],"op",TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"],STATES[-1])
        open_item=self.service.create_item(alarm(ref="AL-OPEN"),"creator",'sensor_operator')
        self.service.register_calibration(cal(factor=3.0,replace=True),"op",'sensor_operator')
        result=self.service.recalculate_point("PT-1",{},"op",'sensor_operator')
        self.assertEqual(result["updated"],[open_item["id"]])
        self.assertEqual(result["skipped_terminal"],[done["id"]])
        done_after=self.service.get_item(done["id"],"viewer")
        self.assertEqual(done_after["factor"],1.0); self.assertEqual(done_after["corrected_quantity"],10.0)
        open_after=self.service.get_item(open_item["id"],"viewer")
        self.assertEqual(open_after["factor"],3.0); self.assertEqual(open_after["corrected_quantity"],30.0)

    def test_recalculate_device_mismatch_is_blocked_not_updated(self):
        self.service.register_calibration(cal(),"op",'sensor_operator')
        item=self.service.create_item(alarm(),"creator",'sensor_operator')
        self.service.register_calibration(cal(device="DEV-2",replace=True),"op",'sensor_operator')
        result=self.service.recalculate_point("PT-1",{},"op",'sensor_operator')
        self.assertEqual(result["updated"],[])
        self.assertEqual(result["blocked"][0]["item_id"],item["id"])
        self.assertIn("设备号对不上",result["blocked"][0]["reasons"][0])
        with self.assertRaisesRegex(ConflictError,"校准缺失"):
            self.service.recalculate_point("PT-NONE",{},"op",'sensor_operator')

    def test_audit_chain_covers_calibration_and_recalculate(self):
        self.service.register_calibration(cal(),"op",'sensor_operator')
        item=self.service.create_item(alarm(),"creator",'sensor_operator')
        self.service.register_calibration(cal(factor=2.0,replace=True),"op",'sensor_operator')
        self.service.recalculate_point("PT-1",{},"op",'sensor_operator')
        actions=[e["action"] for e in self.service.audit("viewer")]
        self.assertIn("calibration_register",actions); self.assertIn("recalculate",actions)
        self.assertTrue(self.repo.verify_audit_chain())

if __name__=="__main__": unittest.main()
