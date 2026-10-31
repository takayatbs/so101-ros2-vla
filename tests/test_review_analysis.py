import copy

import numpy as np
import pytest

from so101_core.review_analysis import enrich, summary, consistency


class StraightLine:
    description = {'available': True, 'model': 'analytic test model', 'base_frame':'base', 'ee_frame':'tip'}

    def evaluate(self, joints, values):
        return None if values is None else {'position':[float(values[0]),0,0], 'rotation':np.eye(3).tolist(), 'links':[[0,0,0],[float(values[0]),0,0]]}


def data(offset=0):
    return {'episode':'test','task':'test','joints':['slide'], 'videos':{},
            'rows':[{'t':float(i),'timestamp_ns':str(i*1_000_000_000),'valid':i!=2,'warnings':[], 'reasons':['gap'] if i==2 else [],
                     'state':[float(i)+offset],'action':[float(i)+offset+.1], 'errors':{}} for i in range(5)]}


def test_metrics_do_not_interpolate_or_differentiate_across_invalid_rows():
    value=enrich(data(),StraightLine())
    assert value['metrics']['ee_path_length_m']==2
    assert value['rows'][2]['ee_speed'] is None
    assert value['rows'][3]['ee_speed'] is None
    assert value['rows'][4]['ee_speed']==1
    assert value['normalized_ee'][50] is None
    assert value['normalized_ee'][25]==[1,0,0]
    assert any(i['kind']=='excluded' and i['t']==2 for i in value['issues'])


def test_consistency_uses_comparable_other_accepted_episodes_only():
    items=[]
    for i,offset in enumerate((0,.1,10)):
        d=enrich(data(offset),StraightLine(),{'success':i<2,'collection_selection':{'accepted':i<2},'robot_hash':'same','calibration_sha256':{'f':'same'}})
        d['episode']=str(i)
        items.append(summary(d))
    consistency(items)
    assert items[0]['consistency_rms_m']==pytest.approx(.1)
    assert items[1]['endpoint_distance_m']==pytest.approx(.1)
    different=copy.deepcopy(items[1]);different['group']='other'
    singleton=[copy.deepcopy(items[0]),different]
    consistency(singleton)
    assert singleton[0]['consistency_rms_m'] is None


def test_recording_tail_outside_common_timeline_is_visible():
    d=enrich(data(),StraightLine(),{'start_time_ns':0,'end_time_ns':5_000_000_000})
    assert d['metrics']['source_coverage_percent']==80
    assert d['metrics']['tail_unaligned_s']==1
    assert any(i['kind']=='coverage' and '末尾' in i['label'] for i in d['issues'])
    assert summary(d)['time_origin_timestamp_ns']=='0'
