"""URDF forward kinematics for offline review; never claims measured TCP accuracy."""
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np


def numbers(value, default):
    result = np.asarray([float(x) for x in value.split()] if value else default, dtype=float)
    if result.shape != (3,) or not np.isfinite(result).all():
        raise ValueError('Expected three finite URDF coordinates')
    return result


def rotation(axis, angle):
    axis = np.asarray(axis, dtype=float)
    norm = np.linalg.norm(axis)
    if norm == 0:
        raise ValueError('URDF joint axis cannot be zero')
    x, y, z = axis/norm
    skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3)+np.sin(angle)*skew+(1-np.cos(angle))*(skew@skew)


def origin_transform(element):
    transform = np.eye(4)
    if element is not None:
        transform[:3, 3] = numbers(element.get('xyz'), [0, 0, 0])
        roll, pitch, yaw = numbers(element.get('rpy'), [0, 0, 0])
        transform[:3, :3] = rotation([0, 0, 1], yaw)@rotation([0, 1, 0], pitch)@rotation([1, 0, 0], roll)
    return transform


class ReviewKinematics:
    def __init__(self, path, root='base_link', tip='gripper_link', tcp=(0, 0, 0), robot_config=None):
        path = Path(path).resolve()
        approximate = 'Approximate primitives' in path.read_text()
        if path.suffix == '.xacro':
            import xacro
            mappings = {'robot_config': str(robot_config)} if robot_config else {}
            xml = xacro.process_file(str(path), mappings=mappings).toxml()
        else:
            xml = path.read_text()
        model = ET.fromstring(xml)
        if root == tip:
            raise ValueError('Base and EE frame must differ')
        parents = {}
        self.joints = {}
        for element in model.findall('joint'):
            parent, child = element.find('parent'), element.find('child')
            if parent is None or child is None:
                continue
            mimic = element.find('mimic')
            joint = {'name': element.get('name'), 'type': element.get('type'),
                     'parent': parent.get('link'), 'child': child.get('link'),
                     'origin': origin_transform(element.find('origin')),
                     'axis': numbers(element.find('axis').get('xyz') if element.find('axis') is not None else None, [1, 0, 0]),
                     'mimic': dict(mimic.attrib) if mimic is not None else None}
            if joint['child'] in parents:
                raise ValueError('URDF has multiple parents for '+joint['child'])
            parents[joint['child']] = joint
            self.joints[joint['name']] = joint
        self.chain = []
        current, seen = tip, set()
        while current != root:
            if current in seen or current not in parents:
                raise ValueError(f'No unambiguous URDF chain from {root} to {tip}')
            seen.add(current)
            joint = parents[current]
            if joint['type'] not in ('fixed', 'revolute', 'continuous', 'prismatic'):
                raise ValueError('Unsupported joint type: '+str(joint['type']))
            self.chain.insert(0, joint)
            current = joint['parent']
        self.tcp = np.asarray(tcp, dtype=float)
        if self.tcp.shape != (3,) or not np.isfinite(self.tcp).all():
            raise ValueError('TCP must contain three finite coordinates in meters')
        self.description = {'available': True, 'model': path.name, 'model_sha256': hashlib.sha256(xml.encode()).hexdigest(),
            'base_frame': root, 'ee_frame': tip, 'tcp_xyz_m': self.tcp.tolist(), 'units': 'm',
            'accuracy': '近似モデル・実機CAD/TCP未校正' if approximate else '指定URDFによる推定・実機との校正状態は未確認'}

    def evaluate(self, joint_names, values):
        if values is None or len(joint_names) != len(values) or any(v is None for v in values) or not np.isfinite(values).all():
            return None
        q = dict(zip(joint_names, values))
        def value(joint, seen=()):
            if joint['name'] in seen:
                raise ValueError('Cyclic mimic joint')
            if joint['mimic']:
                mimic = joint['mimic']
                original = self.joints[mimic['joint']]
                return value(original, (*seen, joint['name']))*float(mimic.get('multiplier', 1))+float(mimic.get('offset', 0))
            if joint['name'] not in q:
                raise ValueError('Recorded joint missing for FK: '+joint['name'])
            return q[joint['name']]
        transform = np.eye(4)
        links = [transform[:3, 3].tolist()]
        for joint in self.chain:
            transform = transform@joint['origin']
            motion = np.eye(4)
            if joint['type'] in ('revolute', 'continuous'):
                motion[:3, :3] = rotation(joint['axis'], value(joint))
            elif joint['type'] == 'prismatic':
                axis = joint['axis']/np.linalg.norm(joint['axis'])
                motion[:3, 3] = axis*value(joint)
            transform = transform@motion
            links.append(transform[:3, 3].tolist())
        position = transform[:3, :3]@self.tcp+transform[:3, 3]
        links.append(position.tolist())
        return {'position': position.tolist(), 'rotation': transform[:3, :3].tolist(), 'links': links}


def load_review_kinematics(config, urdf=None, base='base_link', ee='gripper_link', tcp=(0, 0, 0)):
    default = Path(__file__).resolve().parents[2]/'so101_description/urdf/so101.urdf.xacro'
    # Resolve via package index for an installed (non-symlink) ROS package.
    if not default.exists():
        try:
            from ament_index_python.packages import get_package_share_directory
            default = Path(get_package_share_directory('so101_description'))/'urdf/so101.urdf.xacro'
        except (ImportError, LookupError):
            pass
    try:
        model = ReviewKinematics(urdf or default, base, ee, tcp, config.get('robot', {}).get('_file'))
        # Fail early rather than guessing unrecorded joints as zero.
        model.evaluate(config['robot']['joints'], np.zeros(len(config['robot']['joints'])))
        return model
    except (ValueError, OSError, KeyError, ImportError) as error:
        if urdf:
            raise
        return {'available': False, 'accuracy': 'EEを計算できません', 'reason': str(error)}
