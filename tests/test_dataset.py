import numpy as np
import pytest
from so101_core.dataset import DatasetWriter, SO101Dataset


def test_rgb_lossless_episode_boundaries_padding_and_statistics(tmp_path):
    root=tmp_path/'dataset'
    writer=DatasetWriter(root,'smoke',30,['a'],{'top':(2,3,3)})
    image=np.array([[[255,0,0],[0,255,0],[0,0,255]]]*2,dtype=np.uint8)
    for episode, value in enumerate((1.,100.)):
        for frame in range(3): writer.add_frame([value],[value+frame],{'top':image},frame/30,'task')
        writer.save_episode(str(episode))
    writer.finalize()
    ds=SO101Dataset(root,4,[0])
    frame=ds[1]
    np.testing.assert_array_equal(frame['images']['top'],image)
    np.testing.assert_array_equal(frame['padding'],[False,False,True,True])
    np.testing.assert_array_equal(frame['action'][:,0],[2.,3.,3.,3.])
    assert ds.statistics()['state_mean']==[1.]
    assert len(ds)==3
    with pytest.raises(FileExistsError): DatasetWriter(root,'smoke',30,['a'],{})


def test_native_dataset_rejects_legacy_directory(tmp_path):
    with pytest.raises(ValueError,match='convert original MCAP'): SO101Dataset(tmp_path)
