"""Bounded application events and local diagnostic exports.

Full battle captures remain in each match's events.jsonl.  This module keeps
the desktop event stream small enough to inspect during a live match.
"""

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid


SCHEMA = 'tencent-fl-app-event.v1'
REPORT_SCHEMA = 'tencent-fl-diagnostic.v1'


def _actions(value):
    return [dict(kind=a.get('kind'), card_id=a.get('card_id'), hand_slot=a.get('hand_slot'),
                 target_grid=a.get('target_grid'),ability_id=a.get('ability_id'),
                 source_entity=a.get('source_entity')) for a in value.get('actions', ())
            if a.get('kind') not in ('wait', 'wait_until_affordable')]


def describe_event(event, value, card_names=None):
    """Return severity, category, message, details; None for noisy telemetry."""
    card_names = card_names or {}
    card = lambda card_id: card_names.get(card_id, str(card_id))
    if event == 'probe':
        kind = value.get('event')
        if kind == 'native_partial_frame':
            frame = value['frame']
            if frame.get('mapping_error'):
                return 'warning', 'input', '战场映射异常', dict(tick=frame.get('tick'),error=frame['mapping_error'])
            return None
        if kind in ('elixir_sample', 'deploy_elixir_getter_result', 'native_deploy_validation'):
            return None
        if kind == 'native_deploy_receipt':
            if value.get('stage')=='battle_ended':
                return 'info','deploy','上一局终局时未应用的命令已取消',dict(
                    id=value.get('id'),tick=value.get('tick'),card_id=value.get('card_id'),
                    world=value.get('world'))
            severity = ('warning' if value.get('stage')=='source_despawned' else
                        'info' if value.get('ok') else 'error')
            stage = value.get('stage') or value.get('error', '未知')
            message=('技能未生效：英雄在执行前阵亡' if value.get('stage')=='source_despawned' else f'原生命令：{stage}')
            return severity, 'deploy', message, dict(id=value.get('id'),tick=value.get('tick'),
                card_id=value.get('card_id'),stage=value.get('stage'),error=value.get('error'))
        if kind in ('native_runtime_error', 'probe_error', 'native_public_event_error'):
            return 'error', 'device', f'探针异常：{value.get("message", kind)}', dict(kind=kind,
                message=value.get('message'),stack=value.get('stack'))
        if kind in ('native_runtime_ready', 'opponent_elixir_probe_ready'):
            return 'info', 'device', f'探针就绪：{kind}', dict(kind=kind)
        return None
    if event in ('trial_mapping', 'screenshot', 'trial_evidence'):
        return None
    if event == 'expired_action':
        action=value['action']
        label=(f"技能 {action.get('ability_id')}" if action.get('kind')=='activate_ability'
               else card(action.get('card_id')))
        return 'warning','deploy',f"未提交 {label}：{value['reason']}",dict(
            decision_tick=value['decision_tick'],latest_tick=value['latest_tick'],
            reason=value['reason'],card_id=action.get('card_id'))
    if event == 'card_gap_blocked':
        action=value['action']
        return 'info','deploy',f"未提交 {card(action.get('card_id'))}：最小下牌间隔还剩 {value['remaining_ms']} ms",dict(
            decision_tick=value['decision_tick'],card_id=action.get('card_id'),
            remaining_ms=value['remaining_ms'],min_card_gap_ms=value['min_card_gap_ms'])
    if event == 'decision_pacing':
        if value['interval_ms']<=250:return None
        return 'warning','model',f"推理较慢，决策间隔调整为 {value['interval_ms']} ms",value
    if event in ('battle_decision', 'trial_decision', 'decision'):
        actions = _actions(value)
        if not actions:
            return None
        cards = '、'.join((f"技能 {a['ability_id']} · 实体 {a['source_entity']}" if a['kind']=='activate_ability'
                          else card(a['card_id'])) for a in actions)
        return 'info', 'model', f'模型动作：{cards}', dict(tick=value.get('tick'),
            inference_ms=value.get('inference_ms'),actions=actions)
    if event == 'native_request':
        request = value['request']
        if request.get('kind')=='activate_ability':
            return 'info','deploy',f"已提交技能 {request['ability_id']}，等待客户端执行",dict(
                id=request['id'],kind=request['kind'],source_entity=request['source_entity'],
                ability_id=request['ability_id'],observed_tick=request['observed_tick'],due_tick=request['due_tick'])
        return 'info', 'deploy', f'已提交 {card(request["card_id"])}，等待客户端执行', dict(
            id=request['id'],card_id=request['card_id'],observed_tick=request['observed_tick'],
            due_tick=request['due_tick'],hand_slot=request['hand_slot'],x=request['x'],y=request['y'])
    if event == 'battle_application':
        receipt = value['receipt']
        if receipt.get('kind')=='activate_ability':
            return 'info','deploy',f"客户端已释放技能 {receipt['ability_id']}",dict(
                id=receipt['id'],source_entity=receipt['source_entity'],tick=receipt['tick'],
                charges_before=receipt.get('charges_before'),charges_after=receipt.get('charges_after'))
        return 'info', 'deploy', f'客户端已应用 {card(receipt["card_id"])}', dict(
            id=receipt['id'],card_id=receipt['card_id'],tick=receipt['tick'],applied=value['applied'])
    if event == 'battle_fizzle':
        receipt=value['receipt']
        return 'warning','deploy','英雄阵亡，技能未生效；对局继续',dict(
            id=receipt['id'],tick=receipt['tick'],source_entity=receipt['source_entity'],
            ability_id=receipt['ability_id'])
    if event == 'public_deployment':
        side = value.get('side', '未知')
        return 'info', 'battle', f"{side}公开下牌：{card(value.get('card_id'))}", dict(
            side=side,tick=value.get('tick'),card_id=value.get('card_id'),
            x=value.get('x'),y=value.get('y'),effective_cost=value.get('effective_cost'),
            action_id=value.get('action_id'))
    if event == 'battle_progress':
        return 'info', 'battle', f'运行中：决策 {value["decisions"]} / 实际下牌 {value["applied"]}', dict(
            decisions=value['decisions'],applied=value['applied'],pending=value['pending'],
            directory=value.get('directory'))
    if event == 'connected':
        return 'info', 'device', f'设备已连接，PID {value}', dict(pid=value)
    if event == 'model_ready':
        return 'info', 'model', 'FirstLight 模型已加载', dict(checkpoint_id=value.get('checkpoint_id'),
            sha256=value.get('sha256'))
    if event == 'control_status':
        return 'info', 'battle', str(value), {}
    if event == 'battle_finished':
        return 'info', 'result', '对局已结束，等待结果确认', dict(directory=value.get('directory'),
            reason=value.get('reason'))
    if event == 'match_file':
        return 'info', 'result', '本局 JSONL 已保存：'+str(value), dict(path=str(value))
    if event == 'match_result':
        method='（原生终局）' if value.get('source')=='native_terminal' else ''
        return 'info', 'result', f'结果已记录：{value["result"]}{method}', dict(result=value['result'],
            stats=value.get('stats'),directory=value.get('directory'),source=value.get('source'))
    if event == 'continuous_status':
        return 'info', 'battle', str(value), {}
    if event == 'continuous_tap_result':
        severity='error' if value.get('error') else 'info'
        return severity,'battle',('屏幕点击失败：'+value['error'] if value.get('error') else
            f"已点击{value['kind']}按钮：Android ({value['x']}, {value['y']})"),value
    if event in ('error', 'exception'):
        details = value if isinstance(value, dict) else {'message':str(value)}
        return 'error', 'error', str(details.get('message', value)), details
    if event == 'ui_action':
        return 'info', 'battle', f'操作：{value["name"]}', value
    if event == 'input_report':
        return 'info' if value.get('ready') else 'warning', 'input', (
            '实时输入检查通过' if value.get('ready') else '实时输入仍有缺项'), dict(
                tick=value.get('tick'),ready=value.get('ready'),missing=value.get('missing_labels'))
    if event == 'model_demo':
        return 'info', 'model', '离线模型演示完成', dict(decisions=len(value.get('decisions',())))
    if event == 'diagnostics_exported':
        return 'info', 'result', '诊断报告已导出', dict(path=str(value))
    return 'info', 'battle', str(event), {'summary':str(value)[:300]}


class EventJournal:
    def __init__(self, path, *, recent_limit=500, card_names=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a', encoding='utf-8')
        self.session = uuid.uuid4().hex[:12]
        self.recent = deque(maxlen=recent_limit)
        self.last_signature = None
        self.last_at = 0.0
        self.last_progress_at = 0.0
        self.card_names = card_names or {}

    def record(self, event, value):
        described = describe_event(event, value, self.card_names)
        if described is None:
            return None
        severity, category, message, details = described
        now = time.monotonic()
        if event == 'battle_progress':
            if now-self.last_progress_at < 10:
                return None
            self.last_progress_at = now
        signature = (event, message, details.get('error') if isinstance(details,dict) else None)
        if signature == self.last_signature and now-self.last_at < 10:
            return None
        self.last_signature,self.last_at = signature,now
        row = dict(schema=SCHEMA,at=datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                   session=self.session,severity=severity,category=category,event=event,
                   message=message,details=details)
        self.file.write(json.dumps(row,ensure_ascii=False,default=str)+'\n')
        self.file.flush()
        self.recent.append(row)
        return row

    def close(self):
        self.file.close()


def export_report(root, *, status, config, recent):
    root = Path(root)
    match_directory = Path(config.get('match_directory') or root/'records/matches').expanduser()
    matches = sorted(match_directory.glob('*/summary.json'))
    last = matches[-1] if matches else None
    summary = json.loads(last.read_text()) if last else None
    result_path = last.parent/'terminal-evidence.json' if last else None
    report = dict(schema=REPORT_SCHEMA,created_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        status=status,config={key:config.get(key) for key in ('serial','package','expected_version','checkpoint',
            'model_directory','match_directory','card_names_config',
            'extended_public_mapping','execution_backend')},recent_events=list(recent)[-200:],
        latest_match=summary,latest_match_directory=str(last.parent) if last else None,
        latest_native_result=json.loads(result_path.read_text()) if result_path and result_path.exists() else None,
        full_match_log=(summary.get('labeled_events_file') or str(last.parent/'events.jsonl')) if last else None)
    destination = root/'records/diagnostics'
    destination.mkdir(parents=True, exist_ok=True)
    path = destination/(datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:4]+'.json')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    return path
