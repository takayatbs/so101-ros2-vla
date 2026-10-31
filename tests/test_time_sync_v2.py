import numpy as np
import pytest
from so101_core.synchronization import Sample, Timeline, alignment_settings, aligned_samples


def timeline(times, topic, offset=0):
    return Timeline([Sample(t,[i+offset],round(t*1e9),i,topic) for i,t in enumerate(times)])


def test_source_reversal_and_duplicate_are_not_sorted_away():
    for times in ([.1,.09],[.1,.1]):
        with pytest.raises(ValueError,match='duplicates/reversals'):
            timeline(times,'/state')


def test_causal_selection_target_clock_and_invalid_rows_are_retained():
    states = timeline(np.arange(0,.51,.01),'/state')
    actions = timeline(np.arange(0,.51,.02),'/action',100)
    images = timeline([0,.1,.4,.5],'/camera')
    rows = list(aligned_samples(states,actions,{'top':images},20,alignment_settings({})))
    assert [r['target_ns'] for r in rows] == [i*50_000_000 for i in range(11)]
    assert any(r['warnings'] for r in rows)
    assert [r['index'] for r in rows if not r['valid']] == [4,5,6,7]
    for r in rows:
        assert all(s.timestamp_ns <= r['target_ns'] for s in (r['state'],r['action'],r['images']['top']))
        assert r['errors']['state'] <= 0 and r['errors']['action'] <= 0


def test_nearest_is_explicit_and_exact_requires_hardware_declaration():
    spec = alignment_settings({'synchronization':{'image_selection':'nearest'}})
    rows = list(aligned_samples(timeline([0,.05,.1],'/state'),timeline([0,.1],'/action'),
                                {'top':timeline([0,.06,.1],'/cam')},20,spec))
    assert rows[1]['images']['top'].timestamp_ns == 60_000_000
    assert rows[1]['errors']['image/top'] == pytest.approx(.01)
    with pytest.raises(ValueError,match='hardware'):
        alignment_settings({'synchronization':{'image_selection':'exact'}})


def test_integer_nanoseconds_and_explicit_action_offset():
    origin = 1_792_900_000_000_000_003
    def ns_timeline(topic):
        return Timeline([Sample((origin+i*10_000_000)/1e9,[i],origin+i*10_000_000,i,topic) for i in range(20)])
    spec = alignment_settings({'synchronization':{'action_offset_s':.02}})
    rows = list(aligned_samples(ns_timeline('/state'),ns_timeline('/action'),{'top':ns_timeline('/cam')},30,spec))
    assert rows[0]['target_ns'] == origin
    assert rows[1]['target_ns'] == origin+33_333_333
    assert rows[0]['action'].timestamp_ns == origin+20_000_000
    assert rows[0]['errors']['action'] == .02


def test_operator_inputs_are_causal_and_required_gaps_invalidate_rows():
    source = timeline(np.arange(0,.51,.01),'/state')
    operator = timeline([0,.4,.5],'/leader')
    rows = list(aligned_samples(source,source,{'top':source},20,alignment_settings({}),{'leader':operator},['leader']))
    assert not rows[3]['valid'] and 'input/leader:time_error' in rows[3]['reasons']
    assert rows[3]['inputs']['leader'].timestamp_ns == 0
    with pytest.raises(ValueError,match='operator input'):
        list(aligned_samples(source,source,{'top':source},20,alignment_settings({}),{},['leader']))
