import numpy as np
import pytest

from so101_core.review_kinematics import ReviewKinematics


def test_fk_rotates_link_and_tcp_in_joint_frame(tmp_path):
    path = tmp_path/'arm.urdf'
    path.write_text('''<robot name="test"><link name="base"/><link name="shoulder"/><link name="tool"/>
    <joint name="pan" type="revolute"><parent link="base"/><child link="shoulder"/><origin xyz="0 0 1"/><axis xyz="0 0 1"/></joint>
    <joint name="tool_fixed" type="fixed"><parent link="shoulder"/><child link="tool"/><origin xyz="1 0 0"/></joint></robot>''')
    model = ReviewKinematics(path, 'base', 'tool', (.2, 0, 0))
    pose = model.evaluate(['pan'], [np.pi/2])
    np.testing.assert_allclose(pose['position'], [0, 1.2, 1], atol=1e-12)
    np.testing.assert_allclose(np.array(pose['rotation'])@[1,0,0], [0,1,0], atol=1e-12)
    assert pose['links'][0] == [0,0,0]


def test_fk_rpy_prismatic_and_mimic(tmp_path):
    path = tmp_path/'slide.urdf'
    path.write_text('''<robot name="test"><joint name="slide" type="prismatic"><parent link="base"/><child link="a"/><origin rpy="0 0 1.5707963267948966"/><axis xyz="2 0 0"/></joint>
    <joint name="mirror" type="prismatic"><parent link="a"/><child link="tool"/><axis xyz="1 0 0"/><mimic joint="slide" multiplier="-0.5" offset="0.1"/></joint></robot>''')
    model = ReviewKinematics(path, 'base', 'tool')
    np.testing.assert_allclose(model.evaluate(['slide'], [.4])['position'], [0,.3,0], atol=1e-12)
    with pytest.raises(ValueError, match='missing'):
        model.evaluate(['wrong'], [0])
    assert model.evaluate(['slide'], [None]) is None


def test_fk_rejects_missing_and_cyclic_chains(tmp_path):
    path = tmp_path/'bad.urdf'
    path.write_text('<robot><joint name="one" type="fixed"><parent link="b"/><child link="a"/></joint><joint name="two" type="fixed"><parent link="a"/><child link="b"/></joint></robot>')
    with pytest.raises(ValueError, match='unambiguous'):
        ReviewKinematics(path, 'root', 'a')
