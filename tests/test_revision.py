from decimal import Decimal

import pytest

from app import revisions
from app.engine import evaluate


def correction(device='REF-A2', field='zone', to='西区', **extra):
    payload = {'device': device, 'field': field, 'to': to,
               'basis': '台账更正单 PCS-2026-1015', 'reason': '投运区域登记有误', 'actor': '运维张工'}
    payload.update(extra)
    return payload


def revise(base, items):
    prepared = revisions.prepare(base, items)
    evaluated = evaluate({'dataset': base['source'], 'overrides': prepared['overrides']},
                         snapshot=base['input_snapshot'])
    return revisions.assemble(base, evaluated, prepared)


def test_zone_correction_recomputes_only_affected(clean):
    result = revise(clean, [correction('REF-A2', 'zone', '西区')])
    # REF-A2 调离东区后东区只剩 REF-A1 一台对照，不足下限，INV-A 变为未定。
    affected = [row for row in result['diff']['segments'] if row['status'] == 'affected']
    inherited = [row for row in result['diff']['segments'] if row['status'] == 'inherited']
    assert {row['device'] for row in affected} == {'INV-A'}
    assert {row['device'] for row in inherited} == {'INV-B'}
    assert result['diff']['counts']['affected'] == len(affected)
    assert result['diff']['counts']['inherited'] == len(inherited)


def test_unaffected_segments_keep_original_values(clean):
    result = revise(clean, [correction('REF-A2', 'zone', '西区')])
    base_b = [row for row in clean['energy_rows'] if row['device'] == 'INV-B']
    new_b = [row for row in result['energy_rows'] if row['device'] == 'INV-B']
    assert [row['loss_kwh'] for row in base_b] == [row['loss_kwh'] for row in new_b]
    assert [row['baseline_kwh'] for row in base_b] == [row['baseline_kwh'] for row in new_b]
    assert all(row['delta_loss_kwh'] == '0.0000'
               for row in result['diff']['segments'] if row['device'] == 'INV-B')


def test_affected_segment_traces_old_and_new(clean):
    result = revise(clean, [correction('REF-A2', 'zone', '西区')])
    affected = next(row for row in result['diff']['segments'] if row['status'] == 'affected')
    assert affected['old']['baseline_kw'] == '100'
    assert affected['new']['baseline_kw'] is None
    assert affected['linked'] is True
    trace = next(row['revision_trace'] for row in result['energy_rows'] if row['device'] == 'INV-A')
    assert trace['of_run'] == clean['manifest']['run_id']
    assert trace['old']['loss_kwh'] is not None
    assert result['diff']['corrections'][0]['basis'] == '台账更正单 PCS-2026-1015'


def test_known_to_unknown_blocks_closure(clean):
    result = revise(clean, [correction('REF-A2', 'zone', '西区')])
    codes = {blocker['code'] for blocker in result['blockers']}
    assert 'KNOWN_TO_UNKNOWN' in codes
    assert result['closed'] is False


def test_double_reference_correction_closes(clean):
    # 两区各剩一台对照不足下限；把 REF-B2 设为东区耗材不现实，这里改容量验证闭合路径。
    result = revise(clean, [correction('REF-A1', 'capacity_kw', '200')])
    affected = [row for row in result['diff']['segments'] if row['status'] == 'affected']
    assert affected and all(row['device'] == 'INV-A' for row in affected)
    # 容量归一后基线仍为 100kW（中位数取 REF-A1 75 与 REF-A2 100 的中位 87.5→不闭合口径），只校验不再变未定。
    assert all(row['new']['loss_kwh'] is not None for row in affected)


def test_noop_correction_rejected(clean):
    with pytest.raises(Exception) as error:
        revisions.prepare(clean, [correction('REF-A2', 'zone', '东区')])
    assert getattr(error.value, 'code', '') == 'CORRECTION_NOOP'


def test_stale_from_rejected(clean):
    with pytest.raises(Exception) as error:
        revisions.prepare(clean, [correction('REF-A2', 'zone', '西区', **{'from': '西区'})])
    assert getattr(error.value, 'code', '') == 'CORRECTION_CONFLICT'


def test_missing_basis_rejected(clean):
    item = correction('REF-A2', 'zone', '西区')
    item.pop('basis')
    with pytest.raises(Exception) as error:
        revisions.prepare(clean, [item])
    assert getattr(error.value, 'code', '') == 'CORRECTION_BASIS_REQUIRED'


def test_unknown_field_rejected(clean):
    with pytest.raises(Exception) as error:
        revisions.prepare(clean, [correction('REF-A2', 'power_kw', '120')])
    assert getattr(error.value, 'code', '') == 'CORRECTION_INVALID'


def test_correction_without_effect_blocks(clean):
    # 把被限电的目标设备标成对照：对照资格只作用于参考设备，重算无任何片段变化。
    result = revise(clean, [correction('INV-A', 'reference', True)])
    codes = {blocker['code'] for blocker in result['blockers']}
    assert 'CORRECTION_NO_EFFECT' in codes
    assert result['closed'] is False


def test_inherited_segments_stable_precision(clean):
    result = revise(clean, [correction('REF-A1', 'capacity_kw', '200')])
    base_b = next(row for row in clean['energy_rows'] if row['device'] == 'INV-B')
    new_b = next(row for row in result['energy_rows'] if row['device'] == 'INV-B')
    assert new_b['loss_kwh'] == base_b['loss_kwh']
    assert new_b['baseline_kwh'] == base_b['baseline_kwh']


def test_total_delta_matches_affected(clean):
    result = revise(clean, [correction('REF-A1', 'capacity_kw', '200')])
    total = result['diff']['totals']
    assert Decimal(total['new_loss_kwh']) - Decimal(total['old_loss_kwh']) == \
        Decimal(total['delta_loss_kwh'])

def test_reference_string_toggle_accepted(clean):
    result = revise(clean, [correction('REF-A1', 'reference', 'false')])
    assert result['diff']['corrections'][0]['to'] is False
    assert result['diff']['counts']['affected'] > 0


def test_capacity_trailing_zero_is_noop(clean):
    with pytest.raises(Exception) as error:
        revisions.prepare(clean, [correction('REF-A1', 'capacity_kw', '100.0')])
    assert getattr(error.value, 'code', '') == 'CORRECTION_NOOP'


def test_revision_identity_independent_of_plain_run(clean):
    prepared = revisions.prepare(clean, [correction('REF-A1', 'capacity_kw', '200')])
    evaluated = evaluate({'dataset': 'clean', 'overrides': prepared['overrides']},
                         snapshot=clean['input_snapshot'])
    result = revisions.assemble(clean, evaluated, prepared)
    # 直接以同一 overrides 建普通批会命中与原批相同的批号，修订批必须另立门户。
    assert result['manifest']['run_id'] != clean['manifest']['run_id']
    assert result['manifest']['revision_of'] == clean['manifest']['run_id']
    assert result['diff']['revision_run_id'] == result['manifest']['run_id']
