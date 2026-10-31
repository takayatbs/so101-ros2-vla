"""Describe timestamp provenance without claiming unavailable sensor clocks."""
import json
import os
from pathlib import Path
import socket

import numpy as np
import yaml
from .config import resolve


def stamp_ns(stamp):
    return int(stamp.sec)*1_000_000_000+int(stamp.nanosec)


def source_stamp(msg):
    if hasattr(msg,'timestamp'): return stamp_ns(msg.timestamp)
    if hasattr(msg,'header'): return stamp_ns(msg.header.stamp)
    return None


def timestamp_semantics(topic):
    if topic.startswith('/camera/'):
        return 'host acquisition after V4L2 grab, before decode; sensor exposure timestamp unavailable'
    if topic in ('/hardware/follower/state','/leader/joint_states'):
        return 'host time immediately after servo position read completes'
    if topic == '/follower/joint_states':
        return 'ros2_control publication; prefer /hardware/follower/state for measurement timing'
    if topic == '/follower/applied_action':
        return 'host time after successful servo target submission; physical motion onset not measured'
    return 'message header generation time where available; otherwise unknown'


def clock_info(node):
    boot = Path('/proc/sys/kernel/random/boot_id')
    sim = bool(node.get_parameter('use_sim_time').value)
    return {'ros_clock_type':node.get_clock().clock_type.name,'use_sim_time':sim,
            'epoch':'ROS /clock' if sim else 'Unix epoch nanoseconds',
            'host':socket.gethostname(),'boot_id':boot.read_text().strip() if boot.exists() else None,
            'ros_domain_id':os.environ.get('ROS_DOMAIN_ID','0'),
            'external_clock_sync':'not_verified','hardware_trigger':False,
            'assumption':'USB cameras and servo adapters on this host; remote publishers require independently verified clock synchronization'}


def recording_manifest(node):
    config = node.config
    snapshot = {n:{k:v for k,v in s.items() if not k.startswith('_')} for n,s in config.items()}
    calibration = {}
    for name, pre in config['observation']['preprocessing'].items():
        path = pre.get('undistort',{}).get('calibration')
        p = resolve(config['observation'],path) if path else None
        calibration[name] = {'calibration':yaml.safe_load(p.read_text()) if p is not None and p.is_file() else None,
                             'status':'provided' if p is not None and p.is_file() else 'not_available',
                             'extrinsics':'see recorded /tf and /tf_static; physical calibration not verified'}
    return {'schema':'so101_recording_v2','clock':clock_info(node),'config':snapshot,
            'robot_calibration':{role:json.loads(resolve(config['robot'],config['robot'][role]['calibration']).read_text())
                if resolve(config['robot'],config['robot'][role]['calibration']).is_file() else None
                for role in ('leader','follower')},
            'camera_calibration':calibration,'state_velocity':'finite difference of measured positions; first sample unavailable',
            'state_effort':'not measured; NaN/absent, servo load telemetry is not joint torque',
            'camera_timestamp_source':'host acquisition; hardware exposure timestamp unavailable',
            'message_index':'raw/message_index.jsonl.gz','message_id_scope':'per topic within recorder process',
            'timestamp_fields':{'source_timestamp_ns':'sensor/measurement proxy or generation time, according to timestamp_semantics',
                                'receive_timestamp_ns':'recorder callback entry',
                                'write_call_timestamp_ns':'time immediately before rosbag write; disk commit time unavailable'}}


class MeasuredVelocity:
    def __init__(self): self.previous = None

    def estimate(self, values, timestamp_ns):
        q = np.asarray(values,dtype=float)
        result = np.full(q.shape,np.nan)
        if self.previous is not None:
            previous_ns, previous = self.previous
            dt = (timestamp_ns-previous_ns)/1e9
            if 0 < dt <= .1: result = (q-previous)/dt
        self.previous = timestamp_ns,q.copy()
        return result.tolist()
