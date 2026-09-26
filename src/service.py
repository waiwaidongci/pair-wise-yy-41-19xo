from __future__ import annotations

from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import (ConflictError, ensure_role, normalize_severity,
                     parse_utc_time, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CALIBRATION_ENTITY, CALIBRATION_RECALC_ROLES,
                    CALIBRATION_REGISTER_ROLES, CALIBRATION_VIEW_ROLES,
                    CREATE_ROLES, ENTITY, GATED_TARGETS,
                    RECORD_ROLES, VIEW_ROLES, calibration_active,
                    calibration_blockers, completion_blockers,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ---- 校准评估 ----
    def assess_calibration(self, point_code: Optional[str], device_id: Optional[str],
                           calibration_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """返回测点当前校准生效状态：missing/expired/active/conflict。"""
        if not point_code:
            return None
        now = parse_utc_time(utc_now())
        active = self.repository.list_active_calibrations(point_code, now)
        latest_until = None
        latest_row = self.repository.list_calibrations(point_code)
        if latest_row:
            latest_until = max(cal["valid_until"] for cal in latest_row)
        result: Dict[str, Any] = {
            "point_code": point_code, "device_id": device_id,
            "calibration_id": calibration_id, "current": None,
            "state": "active", "conflict_ids": [], "latest_until": latest_until,
            "device_mismatch": False, "superseded": False,
        }
        if len(active) > 1:
            result["state"] = "conflict"
            result["conflict_ids"] = [cal["id"] for cal in active]
            return result
        if not active:
            result["state"] = "expired" if latest_until else "missing"
            return result
        current = active[0]
        result["current"] = current
        if device_id and current["device_id"] != device_id:
            result["device_mismatch"] = True
        if calibration_id is not None and calibration_id != current["id"]:
            result["superseded"] = True
        return result

    def _gate(self, assessment: Optional[Dict[str, Any]], action: str) -> None:
        blockers = calibration_blockers(assessment, action)
        if blockers:
            if assessment and assessment["state"] == "conflict":
                raise ConflictError("；".join(blockers))
            from .domain import ValidationError
            raise ValidationError("；".join(blockers))

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        raw_quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        point_code = payload.get("point_code")
        if point_code is not None:
            point_code = require_text(point_code, "point_code", 100)
        device_id = payload.get("device_id")
        if device_id is not None:
            device_id = require_text(device_id, "device_id", 100)
        # 新告警放行前必须过校准闸门：缺失/过期/设备号对不上/重复生效均阻断
        assessment = self.assess_calibration(point_code, device_id)
        self._gate(assessment, "create")
        correction_factor = None
        calibration_id = None
        quantity = raw_quantity
        if assessment and assessment["current"]:
            correction_factor = float(assessment["current"]["correction_factor"])
            calibration_id = assessment["current"]["id"]
            quantity = round(raw_quantity * correction_factor, 6)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, point_code,
                                           device_id, raw_quantity, calibration_id,
                                           correction_factor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "raw_quantity": raw_quantity,
            "quantity": quantity, "threshold": threshold,
            "priority": priority_score(severity, quantity, threshold),
            "calibration_id": calibration_id, "correction_factor": correction_factor,
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        # 限行/封闭放行前必须过校准闸门，且必须依据当前生效版本
        if target in GATED_TARGETS:
            assessment = self.assess_calibration(
                item.get("point_code"), item.get("device_id"),
                item.get("calibration_id"))
            self._gate(assessment, "transition")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
            "calibration_id": item.get("calibration_id"),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 校准台账：登记、查询、重算三分开 ----
    def register_calibration(self, payload: Dict[str, Any], actor: str,
                             role: str) -> Dict[str, Any]:
        ensure_role(role, CALIBRATION_REGISTER_ROLES)
        actor = require_text(actor, "actor", 100)
        point_code = require_text(payload.get("point_code"), "point_code", 100)
        device_id = require_text(payload.get("device_id"), "device_id", 100)
        calibrated_at = payload.get("calibrated_at") or utc_now()
        calibrated = parse_utc_time(calibrated_at)
        if calibrated is None:
            from .domain import ValidationError
            raise ValidationError("calibrated_at必须是ISO-8601时间")
        calibrated_at = calibrated.isoformat()
        valid_from = payload.get("valid_from") or calibrated_at
        start = parse_utc_time(valid_from)
        if start is None:
            from .domain import ValidationError
            raise ValidationError("valid_from必须是ISO-8601时间")
        valid_from = start.isoformat()
        valid_until_text = require_text(payload.get("valid_until"), "valid_until", 100)
        end = parse_utc_time(valid_until_text)
        if end is None:
            from .domain import ValidationError
            raise ValidationError("valid_until必须是ISO-8601时间")
        if end <= start:
            from .domain import ValidationError
            raise ValidationError("有效期止必须晚于有效期起")
        valid_until = end.isoformat()
        correction_factor = require_number(
            payload.get("correction_factor"), "correction_factor", 0.000000001)
        note = payload.get("note", "") or ""
        if not isinstance(note, str) or len(note) > 2000:
            from .domain import ValidationError
            raise ValidationError("note不能超过2000个字符")
        replace = bool(payload.get("replace", False))
        now = parse_utc_time(utc_now())
        cal = self.repository.register_calibration(
            point_code, device_id, calibrated_at, valid_from, valid_until,
            correction_factor, note.strip(), actor, replace, now)
        self.repository.append_audit("calibration_register", CALIBRATION_ENTITY,
                                     cal["id"], actor, {
            "point_code": point_code, "device_id": device_id,
            "calibrated_at": calibrated_at, "valid_from": valid_from,
            "valid_until": valid_until, "correction_factor": correction_factor,
            "replaced_ids": cal["superseded_ids"],
        })
        for old_id in cal["superseded_ids"]:
            self.repository.append_audit("calibration_supersede", CALIBRATION_ENTITY,
                                         old_id, actor, {
                "point_code": point_code, "superseded_by": cal["id"],
            })
        return self.get_calibration(cal["id"], role)

    def get_calibration(self, calibration_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, CALIBRATION_VIEW_ROLES)
        cal = self.repository.get_calibration(calibration_id)
        return self._calibration_view(cal)

    def list_calibrations(self, role: str,
                          point_code: Optional[str] = None) -> List[Dict[str, Any]]:
        ensure_role(role, CALIBRATION_VIEW_ROLES)
        now = parse_utc_time(utc_now())
        active_map = self.repository.list_all_active_by_point(now)
        result = []
        for cal in self.repository.list_calibrations(point_code):
            view = self._calibration_view(cal)
            active = active_map.get(cal["point_code"], [])
            if cal["state"] == "active" and calibration_active(cal, now):
                view["effective"] = len(active) == 1
                if len(active) > 1:
                    view["conflict_with"] = [c["id"] for c in active if c["id"] != cal["id"]]
            else:
                view["effective"] = False
            result.append(view)
        return result

    def _calibration_view(self, cal: Dict[str, Any]) -> Dict[str, Any]:
        now = parse_utc_time(utc_now())
        view = dict(cal)
        view["effective_now"] = cal["state"] == "active" and calibration_active(cal, now)
        return view

    def recalibrate(self, payload: Optional[Dict[str, Any]], actor: str,
                    role: str) -> Dict[str, Any]:
        """独立重算用例：补录合格校准后，对未结束告警按新系数重算，历史结论保留。"""
        ensure_role(role, CALIBRATION_RECALC_ROLES)
        actor = require_text(actor, "actor", 100)
        payload = payload or {}
        point_code = payload.get("point_code")
        if point_code is not None:
            point_code = require_text(point_code, "point_code", 100)
        recalculated: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        if point_code is None:
            points = sorted({c["point_code"] for c in self.repository.list_calibrations()})
        else:
            points = [point_code]
        for code in points:
            assessment = self.assess_calibration(code, None)
            items = self.repository.list_open_items_by_point(code)
            for item in items:
                if assessment is None:
                    continue
                if assessment["state"] == "conflict":
                    skipped.append({"item_id": item["id"], "point_code": code,
                                    "reason": "重复生效校准，冲突编号：" +
                                    ",".join(str(c) for c in assessment["conflict_ids"])})
                    continue
                current = assessment["current"]
                if current is None:
                    skipped.append({"item_id": item["id"], "point_code": code,
                                    "reason": "无生效校准（" + assessment["state"] + "）"})
                    continue
                old_factor = item.get("correction_factor")
                if item.get("calibration_id") == current["id"] and (
                        old_factor is not None and
                        float(old_factor) == float(current["correction_factor"])):
                    skipped.append({"item_id": item["id"], "point_code": code,
                                    "reason": "已依据生效校准，无需重算"})
                    continue
                raw = item.get("raw_quantity")
                if raw is None:
                    raw = item["quantity"]
                new_quantity = round(float(raw) * float(current["correction_factor"]), 6)
                old = {"quantity": item["quantity"],
                       "calibration_id": item.get("calibration_id"),
                       "correction_factor": old_factor,
                       "device_id": item.get("device_id")}
                self.repository.apply_item_calibration(
                    item["id"], new_quantity, current["id"],
                    float(current["correction_factor"]), actor)
                self.repository.append_audit("recalibrate", ENTITY, item["id"], actor, {
                    "point_code": code, "raw_quantity": raw,
                    "old_quantity": old["quantity"], "new_quantity": new_quantity,
                    "old_calibration_id": old["calibration_id"],
                    "new_calibration_id": current["id"],
                    "old_correction_factor": old_factor,
                    "new_correction_factor": current["correction_factor"],
                    "old_device_id": old["device_id"],
                    "new_device_id": current["device_id"],
                    "status_unchanged": item["status"],
                })
                recalculated.append({
                    "item_id": item["id"], "point_code": code,
                    "old_quantity": old["quantity"], "new_quantity": new_quantity,
                    "old_calibration_id": old["calibration_id"],
                    "calibration_id": current["id"],
                    "correction_factor": current["correction_factor"],
                    "device_id": current["device_id"],
                    "priority_before": priority_score(
                        item["severity"], item["quantity"], item["threshold"]),
                    "priority_after": priority_score(
                        item["severity"], new_quantity, item["threshold"]),
                })
        return {"recalculated": recalculated, "skipped": skipped}

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        # 生效版本与受阻原因（查询，不放行也不阻断）
        assessment = self.assess_calibration(
            item.get("point_code"), item.get("device_id"),
            item.get("calibration_id"))
        if assessment is None:
            result["calibration"] = {"state": "unbound", "current": None,
                                     "blockers_create": [], "blockers_restriction": []}
            return result
        current = assessment["current"]
        result["calibration"] = {
            "state": assessment["state"],
            "calibration_id": item.get("calibration_id"),
            "effective_calibration_id": current["id"] if current else None,
            "device_id": current["device_id"] if current else None,
            "correction_factor": current["correction_factor"] if current else None,
            "valid_until": current["valid_until"] if current else assessment["latest_until"],
            "conflict_ids": assessment["conflict_ids"],
            "device_mismatch": assessment["device_mismatch"],
            "version_stale": assessment["superseded"],
            "blockers_create": calibration_blockers(assessment, "create"),
            "blockers_restriction": calibration_blockers(assessment, "transition"),
        }
        return result
