from __future__ import annotations
from .domain import ConflictError, ValidationError, parse_utc_time
TITLE='桥梁结构监测与限行决策'; ENTITY='桥梁告警'; ID_PREFIX='BM'
CALIBRATION_ENTITY='校准台账'
SEVERITIES=['normal', 'watch', 'warning', 'critical']; STATES=['normal', 'warning', 'restricted', 'closed', 'restored']; TRANSITIONS={'normal': ['warning'], 'warning': ['restricted'], 'restricted': ['closed'], 'closed': ['restored'], 'restored': []}; TRANSITION_ROLES={'warning': ['sensor_operator'], 'restricted': ['bridge_engineer'], 'closed': ['traffic_authority'], 'restored': ['bridge_engineer']}
CREATE_ROLES=set(['sensor_operator']); RECORD_ROLES=set(['sensor_operator', 'bridge_engineer']); AUDIT_ROLES=set(['bridge_engineer', 'viewer']); VIEW_ROLES=set(['sensor_operator', 'bridge_engineer', 'traffic_authority', 'viewer'])
CALIBRATION_REGISTER_ROLES=set(['sensor_operator', 'bridge_engineer']); CALIBRATION_RECALC_ROLES=set(['bridge_engineer']); CALIBRATION_VIEW_ROLES=set(['sensor_operator', 'bridge_engineer', 'traffic_authority', 'viewer'])
GATED_TARGETS=set(['restricted', 'closed'])
OPEN_STATES=set(['normal', 'warning', 'restricted', 'closed'])
SEVERITY_WEIGHT={'normal': 1.0, 'watch': 3.0, 'warning': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'normal': 72, 'watch': 24, 'warning': 8, 'critical': 4}; TERMINAL_STATES=set(['restored'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def calibration_active(cal, now):
    start=parse_utc_time(cal["valid_from"]); end=parse_utc_time(cal["valid_until"])
    return cal["state"]=="active" and start is not None and end is not None and start<=now<=end
def windows_overlap(start_a,end_a,start_b,end_b):
    return max(start_a,start_b)<=min(end_a,end_b)
def calibration_blockers(assessment, action):
    """评估校准闸门。action: create|transition；返回受阻原因列表。"""
    if assessment is None: return []
    if assessment["state"]=="conflict":
        return ["校准台账存在重复生效记录，冲突编号："+",".join(str(c) for c in assessment["conflict_ids"])]
    if not assessment["current"]:
        if assessment["state"]=="missing": return ["测点缺失校准记录"]
        if assessment["state"]=="expired": return [f"校准记录已过有效期（有效期至{assessment['latest_until']}）"]
        return ["当前无生效校准记录"]
    if assessment["device_mismatch"]:
        current=assessment["current"]
        return [f"设备号对不上：告警绑定{assessment['device_id']}，生效校准设备号{current['device_id']}（校准编号{current['id']}）"]
    if action=="transition" and assessment["superseded"]:
        return [f"系数已过期：当前依据校准编号{assessment['calibration_id']}，生效版本为校准编号{assessment['current']['id']}，请先执行重算"]
    return []
