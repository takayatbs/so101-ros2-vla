import json
import re

import numpy as np
import pytest

from so101_core.recording_review import generate_existing
from so101_core.review_import import local_child


def test_dataset_import_keeps_frame_times_and_video_bytes(tmp_path):
    from test_video_dataset_v2 import make_video_dataset
    config, source, _ = make_video_dataset(tmp_path)
    output=tmp_path/'review'
    generate_existing(source,output,config)
    serial = tmp_path/'serial-review'
    generate_existing(source,serial,config,workers=1)
    parallel = tmp_path/'parallel-review'
    generate_existing(source,parallel,config,workers=2)
    for path in serial.rglob('*'):
        if path.is_file() and path.name != 'generation.json':
            assert path.read_bytes() == (parallel/path.relative_to(serial)).read_bytes()
    assert json.loads((parallel/'generation.json').read_text())['workers'] == 2
    assert (output/'index.html').is_file()
    manifest=json.loads((output/'review_manifest.json').read_text())
    assert len(manifest['episodes'])==2
    for number in range(2):
        original=source/f'episodes/{number:06d}'
        rendered=output/f'episodes/{number:06d}'
        with np.load(original/'frames.npz',allow_pickle=False) as frames:
            expected=frames['timestamp_ns'].copy()
        page=(rendered/'review.html').read_text()
        data=json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>',page,re.S).group(1))
        assert [int(r['timestamp_ns']) for r in data['rows']]==expected.tolist()
        for name in ('top','arm'):
            assert (original/f'videos/{name}.mp4').read_bytes()==(rendered/f'videos/{name}.mp4').read_bytes()
        assert (rendered/'assets/review.js').is_file()


def test_import_paths_cannot_leave_bundle(tmp_path):
    with pytest.raises(ValueError,match='escapes'):
        local_child(tmp_path,'../outside.html')
