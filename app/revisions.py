"""设备归属更正后的修订归档。

两个目标同时成立：
1. 为了赶当天口径，修订采用整批重算（最快）；
2. 同一批未受影响的片段必须沿用原批的值，保证复核可比性。

做法是“全批重算 + 片段级回植”：先按更正后的上下文完整复算，再逐片段与原批
比对。计算值未变化的片段直接回植原批的计算值（原值、原精度），证据字段仍取
更正后台账并在差异表标注；只有计算值变化的片段才采用新结果，并挂上原批值、
更正依据和变化字段，形成可追溯链。

差异是否闭合由 blockers 判定。未闭合的修订只能作为草案保存，不能替换正式
版本；旧正式批始终不可变、可继续导出核验。
"""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json

from app.coerce import number
from app.errors import BusinessError, require

CORRECTABLE = {'zone', 'capacity_kw', 'reference'}
# 参与“受影响”判定的计算值。reference_ids 是证据而非数值：证据变了但口径没变，
# 仍视为未受影响片段，以免把可比性打散。
VALUE_FIELDS = ['baseline_kw', 'actual_kw', 'baseline_kwh', 'loss_kwh', 'code']
EVIDENCE_FIELDS = ['reference_ids']
TRACE_FIELDS = VALUE_FIELDS + EVIDENCE_FIELDS


def _numeric(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except Exception:
        return None


def _same(left, right):
    if isinstance(left, list) or isinstance(right, list):
        return sorted(str(x) for x in (left or [])) == sorted(str(x) for x in (right or []))
    a, b = _numeric(left), _numeric(right)
    if a is not None and b is not None:
        return a == b
    return left == right


def _text(value):
    return str(value).strip() if value is not None else ''


def _normalize_value(field, value):
    if field == 'zone':
        require(isinstance(value, str) and value.strip(), 'CORRECTION_INVALID', '区域归属不能为空')
        return value.strip()
    if field == 'capacity_kw':
        try:
            parsed = number(value)
        except BusinessError:
            raise BusinessError('CORRECTION_INVALID', '额定功率须为正数')
        require(parsed > 0, 'CORRECTION_INVALID', '额定功率须为正数')
        # 与台账原值同口径：去掉多余的尾随零，避免 200 与 200.0 被当成不同更正。
        return format(parsed.normalize(), 'f')
    if value is None or value == '':
        raise BusinessError('CORRECTION_INVALID', '对照资格须为布尔值')
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    if text in {'true', '1', '是', '对照'}:
        return True
    if text in {'false', '0', '否', '非对照'}:
        return False
    raise BusinessError('CORRECTION_INVALID', '对照资格须为布尔值')


def prepare(base, corrections, require_formal=True):
    """校验更正依据，产出上下文补丁。from 与原批上下文不符即拒绝（陈旧更正）。"""
    require('input_snapshot' in base, 'INPUT_SNAPSHOT_MISSING', '旧批缺少源快照，请重新导入')
    if require_formal and (base.get('formal', 1) != 1 or base.get('superseded_by')):
        raise BusinessError('BASE_NOT_FORMAL', '仅可基于当前正式版本发起修订', 409)
    items = corrections if isinstance(corrections, list) else [corrections]
    require(bool(items) and all(isinstance(item, dict) for item in items), 'BAD_CORRECTION', '更正依据须为对象')
    context = deepcopy(base['input_snapshot']['context'])
    normalized, patch = [], {}
    for raw in items:
        device = _text(raw.get('device'))
        require(device in context['devices'], 'UNKNOWN_DEVICE', '更正设备不存在')
        field = raw.get('field')
        require(field in CORRECTABLE, 'CORRECTION_INVALID', '更正字段须为 zone/capacity_kw/reference')
        basis = _text(raw.get('basis'))
        reason = _text(raw.get('reason'))
        actor = _text(raw.get('actor'))
        require(basis, 'CORRECTION_BASIS_REQUIRED', '必须填写更正依据')
        require(reason, 'CORRECTION_BASIS_REQUIRED', '必须填写更正原因')
        require(actor, 'CORRECTION_BASIS_REQUIRED', '必须填写经办人')
        current = context['devices'][device].get(field)
        if 'from' in raw and raw['from'] is not None:
            require(_same(current, raw['from']), 'CORRECTION_CONFLICT',
                    f'更正原值与当前台账不一致，当前为 {current}')
        target = _normalize_value(field, raw.get('to'))
        require(not _same(current, target), 'CORRECTION_NOOP', '更正前后一致，无需修订')
        context['devices'][device][field] = target
        patch.setdefault('devices', {}).setdefault(device, {})[field] = target
        item = {'device': device, 'field': field, 'from': current, 'to': target,
                'basis': basis, 'reason': reason, 'actor': actor}
        item['correction_id'] = hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        normalized.append(item)
    corpus = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    # overrides 走引擎的顶层浅覆盖，因此 devices 必须给出更正后的完整台账。
    return {'base_id': base['manifest']['run_id'], 'corrections': normalized,
            'patch': patch, 'overrides': {'devices': deepcopy(context['devices'])},
            'correction_sha256': corpus}


def _key(row):
    return (row.get('device'), row.get('start'), row.get('end'))


def _label(key):
    return f'{key[0]} {key[1]}~{key[2]}'


def _links(old, new, correction, sample_devices):
    """片段变化是否可由该条更正解释：目标设备自身或其对照证据涉及该设备。"""
    if _key(new)[0] == correction['device']:
        return True
    references = set(old.get('reference_ids') or []) | set(new.get('reference_ids') or [])
    return any(sample_devices.get(sample) == correction['device'] for sample in references)


def _strip_trace(row):
    return {key: value for key, value in row.items() if key != 'revision_trace'}


def _refresh_views(state):
    """回植后按最终片段重算视图与四账，展示与归档判定都以回植结果为准。"""
    from app.modules.reconcile.rules import reconcile
    from app.modules.archive.rules import archive_decision
    rows = state['energy_rows']
    total = sum((number(row['loss_kwh']) for row in rows if row['loss_kwh'] is not None), Decimal(0))
    state['views']['loss'] = {
        'title': '损失电量',
        'rows': [{k: v for k, v in row.items()
                  if k not in ['sample_at', 'zone', 'irradiance', 'status', 'revision_trace']} for row in rows],
        'metrics': {'已确定损失_kWh': str(total.quantize(Decimal('.0001'))),
                    '未定片段': sum(row['loss_kwh'] is None for row in rows)}}
    estimates = state.get('estimates', [])
    state['views']['baseline'] = {
        'title': '对照基线', 'rows': estimates,
        'metrics': {'可估计': sum(r['code'] == 'ESTIMATED' for r in estimates),
                    '未定': sum(r['code'] != 'ESTIMATED' for r in estimates),
                    '对照下限': state['context']['min_references']}}
    state['balance'] = reconcile(state)
    state['views']['reconcile'] = {
        'title': '时间与能量四账', 'rows': [state['balance']],
        'metrics': {'确定损失_kWh': state['balance']['loss_kwh'],
                    '未定分钟': state['balance']['unknown_seconds'] // 60,
                    '并集分钟': state['balance']['union_seconds'] // 60}}
    state['archive'] = archive_decision(state)
    state['views']['archive'] = {
        'title': '运行凭证', 'rows': state['event_unions'],
        'metrics': {'切片': state['archive']['segments'], '未定': state['archive']['unknown'],
                    '状态': state['archive']['status']}}


def assemble(base, evaluated, prepared):
    """把重算结果组装成修订：回植未受影响片段，生成差异与闭合判定。"""
    require(base.get('energy_rows') is not None, 'INCOMPLETE_RUN', '原批缺少损失结果')
    require(evaluated.get('energy_rows') is not None, 'INCOMPLETE_RUN', '修订批缺少损失结果')
    base_rows, new_rows = base['energy_rows'], evaluated['energy_rows']
    old_map = {_key(row): row for row in base_rows}
    new_map = {_key(row): row for row in new_rows}
    order = [_key(row) for row in new_rows] + [key for key in old_map if key not in new_map]
    corrections = prepared['corrections']
    linked_counter = {item['correction_id']: 0 for item in corrections}
    blockers, segments, final_rows, final_estimates = [], [], [], []
    base_est = {_key(row): row for row in base.get('estimates', [])}
    new_est = {_key(row): row for row in evaluated.get('estimates', [])}
    # 证据里存的是采样编号，关联更正需要还原到设备。
    sample_devices = {row.get('id'): row.get('device') for row in evaluated.get('records', [])}
    sample_devices.update({row.get('id'): row.get('device') for row in base.get('records', [])})
    affected = inherited = 0
    delta_accumulator = Decimal(0)

    for key in order:
        old, new = old_map.get(key), new_map.get(key)
        linked, changed = [], []
        evidence_changed = []
        if old is None:
            status = 'added'
            row = deepcopy(new)
            blockers.append({'code': 'STRUCTURE_CHANGED', 'detail': '修订批新增片段', 'segment': _label(key)})
        elif new is None:
            status = 'removed'
            row = _strip_trace(deepcopy(old))
            blockers.append({'code': 'STRUCTURE_CHANGED', 'detail': '修订批丢失片段', 'segment': _label(key)})
        else:
            changed = [field for field in VALUE_FIELDS if not _same(old.get(field), new.get(field))]
            linked = [item['correction_id'] for item in corrections if _links(old, new, item, sample_devices)]
            if changed:
                status = 'affected'
                affected += 1
                # 变化片段采用新基线/新损失，同时保留原批旧值作为追溯依据。
                row = {**deepcopy(old), **deepcopy(new)}
                row['revision_trace'] = {
                    'of_run': prepared['base_id'],
                    'correction_ids': linked,
                    'status': 'affected',
                    'old': {field: old.get(field) for field in TRACE_FIELDS},
                    'changed': changed}
                old_loss, new_loss = old.get('loss_kwh'), new.get('loss_kwh')
                if old_loss is not None and new_loss is None:
                    blockers.append({'code': 'KNOWN_TO_UNKNOWN', 'detail': '原批已确定损失，修订后变为未定',
                                     'segment': _label(key)})
                if not linked:
                    blockers.append({'code': 'UNEXPLAINED_CHANGE', 'detail': '片段变化无法由更正依据解释',
                                     'segment': _label(key)})
                for correction_id in linked:
                    linked_counter[correction_id] += 1
            else:
                status = 'inherited'
                inherited += 1
                # 未受影响片段严格沿用原批计算值：原值、原精度、原证据不动；
                # 区域等上下文派生的元数据仍采用更正后的口径。
                evidence_changed = [field for field in EVIDENCE_FIELDS
                                    if not _same(old.get(field), new.get(field))]
                row = deepcopy(new)
                # 只回植计算值，不改证据：证据必须如实反映更正后的台账，变化在差异表标注。
                for field in VALUE_FIELDS:
                    if field in old:
                        row[field] = deepcopy(old[field])
        if status != 'removed':
            final_rows.append(row)
        old_loss, new_loss = (old or {}).get('loss_kwh'), (new or {}).get('loss_kwh')
        delta = (number(new_loss) if new_loss is not None else Decimal(0)) - \
                (number(old_loss) if old_loss is not None else Decimal(0))
        delta_accumulator += delta
        segments.append({
            'device': key[0], 'start': key[1], 'end': key[2], 'seconds': (new or old).get('seconds'),
            'status': status, 'changed': changed, 'linked': bool(linked),
            # 证据集合变化但计算值不变时不算受影响，仅标注供复核参考。
            'evidence_changed': evidence_changed if status == 'inherited' else [],
            'old': {field: (old or {}).get(field) for field in TRACE_FIELDS},
            'new': {field: (new or {}).get(field) for field in TRACE_FIELDS},
            'delta_loss_kwh': (str(delta.quantize(Decimal('.0001'))) if status in {'affected', 'added', 'removed'} else '0.0000')})

        candidate = new_est.get(key) or base_est.get(key)
        if candidate is not None and status != 'removed':
            if status == 'inherited' and key in base_est:
                # 与 energy 行一致：计算值沿用原批，对照证据取更正后的台账。
                merged = deepcopy(candidate)
                for field in ['baseline_kw', 'code']:
                    if field in base_est[key]:
                        merged[field] = deepcopy(base_est[key][field])
                final_estimates.append(merged)
            else:
                final_estimates.append(_strip_trace(deepcopy(candidate)))

    evaluated['energy_rows'] = final_rows
    evaluated['estimates'] = final_estimates
    _refresh_views(evaluated)

    # 复核隔离与原始账不应受上下文更正影响；变化即视为差异未解释。
    base_quarantine = sorted(json.dumps({k: v for k, v in row.items() if k != 'raw'}, sort_keys=True, ensure_ascii=False)
                             for row in base.get('quarantine', []))
    new_quarantine = sorted(json.dumps({k: v for k, v in row.items() if k != 'raw'}, sort_keys=True, ensure_ascii=False)
                            for row in evaluated.get('quarantine', []))
    if base_quarantine != new_quarantine:
        blockers.append({'code': 'QUARANTINE_CHANGED', 'detail': '更正改变了隔离账，需人工核验'})

    if affected == 0 and not any(item['code'] == 'STRUCTURE_CHANGED' for item in blockers):
        blockers.append({'code': 'NO_AFFECTED_SEGMENT', 'detail': '更正未影响任何片段，修订无意义'})
    for item in corrections:
        if linked_counter[item['correction_id']] == 0:
            blockers.append({'code': 'CORRECTION_NO_EFFECT', 'detail': f"更正未影响任何片段：{item['device']}.{item['field']}",
                             'segment': f"{item['device']}.{item['field']}"})

    old_total = sum((number(row['loss_kwh']) for row in base_rows if row['loss_kwh'] is not None), Decimal(0))
    new_total = sum((number(row['loss_kwh']) for row in final_rows if row['loss_kwh'] is not None), Decimal(0))
    quant = lambda value: value.quantize(Decimal('.0001'))
    # 用未量化增量累计核对总额，避免逐段 0.0001 量化制造尾差。
    if quant(new_total - old_total) != quant(delta_accumulator):
        blockers.append({'code': 'UNEXPLAINED_TOTAL', 'detail': '总损失变化与受影响片段增量不一致'})

    balance = evaluated['balance']
    if not (balance['row_balanced'] and balance['time_balanced'] and balance['energy_balanced']):
        blockers.append({'code': 'CONSERVATION_FAILED', 'detail': '修订批行/时/能量守恒未通过'})
    if balance['unknown_segments'] > base.get('balance', {}).get('unknown_segments', 0):
        blockers.append({'code': 'UNKNOWN_INCREASED', 'detail': '修订后未定片段增加'})
    if base.get('archive', {}).get('status') != '可归档':
        blockers.append({'code': 'BASE_NOT_READY', 'detail': '原批尚未达到可归档状态'})

    # 阻断项去重，保留稳定的展示顺序。
    seen, unique = set(), []
    for blocker in blockers:
        marker = (blocker['code'], blocker.get('segment'), blocker['detail'])
        if marker not in seen:
            seen.add(marker)
            unique.append(blocker)

    diff = {
        'base_run_id': prepared['base_id'], 'revision_run_id': evaluated['manifest']['run_id'],
        'corrections': prepared['corrections'], 'correction_sha256': prepared['correction_sha256'],
        'segments': segments,
        'counts': {'affected': affected, 'inherited': inherited, 'total': len(order)},
        'totals': {
            'old_loss_kwh': str(quant(old_total)), 'new_loss_kwh': str(quant(new_total)),
            'delta_loss_kwh': str(quant(new_total - old_total)),
            'old_unknown_segments': base.get('balance', {}).get('unknown_segments', 0),
            'new_unknown_segments': balance['unknown_segments']},
        'blockers': unique, 'closed': not unique}
    # 修订批按“原批 + 组装后结果”内容寻址：同一更正可复现同一版本，
    # 不同原批或不同片段会让批号不同，不能靠单次重算的源摘要冒充。
    identity = hashlib.sha256(json.dumps({
        'base': prepared['base_id'], 'base_sha256': base['manifest']['sha256'],
        'correction_sha256': prepared['correction_sha256'],
        'segments': [[seg['device'], seg['start'], seg['end'], seg['status'], seg['new']] for seg in segments],
        'totals': diff['totals'], 'closed': diff['closed']}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    evaluated['manifest']['run_id'] = identity[:16]
    evaluated['manifest']['sha256'] = identity
    evaluated['manifest']['revision_of'] = prepared['base_id']
    diff['revision_run_id'] = identity[:16]
    evaluated['revision_info'] = {
        'of_run': prepared['base_id'], 'corrections': prepared['corrections'],
        'correction_sha256': prepared['correction_sha256']}
    evaluated['diff'] = diff
    evaluated['blockers'] = unique
    evaluated['closed'] = diff['closed']
    return evaluated
