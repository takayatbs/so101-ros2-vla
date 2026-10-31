import argparse
import hashlib
import gzip
import json
import shutil
import uuid
from pathlib import Path

import numpy as np
from .config import load_system, config_hash, calibration_hashes
from .dataset import VideoDatasetWriter, VIDEO_FORMAT
from .control import reorder
from .preprocessing import ImagePreprocessor, image_from_ros
from .synchronization import Sample, Timeline, aligned_samples, alignment_settings, timeline_report
from .video_storage import VideoArchive


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _source_index(folder, needed):
    path = folder/'raw/message_index.jsonl.gz'
    if not path.exists(): path = folder/'raw/message_index.jsonl'
    result = {}
    if path.exists():
        with (gzip.open(path,'rt',encoding='utf-8') if path.suffix == '.gz' else path.open()) as stream:
            for line in stream:
                item = json.loads(line)
                key = item.get('source_timestamp_ns')
                if item['topic_name'] in needed and key is not None:
                    identity = (item['topic_name'], key)
                    if identity in result:
                        raise ValueError('Duplicate source timestamp in message index: '+str(identity))
                    result[identity] = item
    return result


def read_episode(folder, config, video_folder, include_failed=False):
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from rosidl_runtime_py.utilities import get_message
    folder = Path(folder)
    metadata = json.loads((folder/'episode.json').read_text())
    if metadata.get('status') != 'complete':
        raise ValueError(f'Incomplete episode: {folder}')
    if not metadata['success'] and not include_failed:
        raise ValueError(f'Failed episode requires --include-failed: {folder}')
    selection_path = folder/'collection.json'
    if selection_path.exists():
        selection = json.loads(selection_path.read_text())
        if not selection.get('accepted', False) and not include_failed:
            raise ValueError(f'Episode excluded by collection selection; requires --include-failed: {folder}')
        metadata['collection_selection'] = selection
    if metadata.get('robot_hash') != config_hash(config['robot']):
        raise ValueError('Robot/units configuration differs from the recorded episode')
    if metadata.get('calibration_sha256', calibration_hashes(config['robot'])) != calibration_hashes(config['robot']):
        raise ValueError('Calibration differs from the recorded episode')
    recording_path = folder/'recording.json'
    recording = json.loads(recording_path.read_text()) if recording_path.exists() else {'clock': {'status':'legacy recording; source clock provenance unavailable'}}
    spec, joints = config['dataset'], config['robot']['joints']
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(folder/'raw'),storage_id='mcap'), rosbag2_py.ConverterOptions('cdr','cdr'))
    available = {t.name:t.type for t in reader.get_all_topics_and_types()}
    camera_topics = {}
    for name in config['observation']['cameras']:
        if not name or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in name):
            raise ValueError('Invalid camera name')
        candidates = [spec['camera_topics'][name],f'/camera/{name}/video',f'/camera/{name}/image_raw']
        topic = next((t for t in candidates if available.get(t) in ('sensor_msgs/msg/Image','foxglove_msgs/msg/CompressedVideo')),None)
        if topic is None: raise ValueError('Missing camera topic: '+name)
        camera_topics[topic] = name
    state_candidates = list(dict.fromkeys([spec.get('measurement_state_topic','/hardware/follower/state'),spec['state_topic'],'/follower/joint_states']))
    state_topics = [t for t in state_candidates if available.get(t) == 'sensor_msgs/msg/JointState']
    frame_meta_topics = {f'/camera/{name}/frame_metadata':name for name in camera_topics.values()
                         if available.get(f'/camera/{name}/frame_metadata') == 'std_msgs/msg/String'}
    frame_metadata = {name:{} for name in camera_topics.values()}
    input_topics = {topic:name for name,topic in spec.get('input_topics',{}).items() if topic in available}
    input_lists = {name:[] for name in spec.get('input_topics',{})}
    needed = {*state_topics,spec['action_topic'],*camera_topics,*frame_meta_topics,*input_topics}
    if spec['action_topic'] not in available: raise ValueError('Missing applied action topic')
    types = {t:get_message(available[t]) for t in needed}
    reader.set_filter(rosbag2_py.StorageFilter(topics=list(needed)))
    indices = _source_index(folder,needed)
    state_lists, actions = {t:[] for t in state_topics}, []
    cameras = {n:[] for n in camera_topics.values()}
    archives, video_info = {}, {}
    counters, last_ns = {}, {}
    start = metadata.get('start_time_ns',round(metadata['start_time']*1e9))
    end = metadata.get('end_time_ns',round(metadata['end_time']*1e9))
    retained = 0
    try:
        while reader.has_next():
            topic, data, receive_ns = reader.read_next()
            ordinal = counters.get(topic,0)
            counters[topic] = ordinal+1
            msg = deserialize_message(data,types[topic])
            if topic in frame_meta_topics:
                item = json.loads(msg.data)
                frame_metadata[frame_meta_topics[topic]][item['timestamp_ns']] = item
                continue
            video = available[topic] == 'foxglove_msgs/msg/CompressedVideo'
            stamp = msg.timestamp if video else msg.header.stamp
            stamp_ns = stamp.sec*1_000_000_000+stamp.nanosec
            if topic in last_ns and stamp_ns <= last_ns[topic]:
                raise ValueError(f'Duplicate/reversed source timestamp: {topic}, {stamp_ns} <= {last_ns[topic]}')
            last_ns[topic] = stamp_ns
            if stamp_ns > end: continue
            identity = indices.get((topic,stamp_ns),{})
            message_id = identity.get('message_id',ordinal)
            receive_ns = identity.get('receive_timestamp_ns',receive_ns)
            frame_id = msg.frame_id if video else msg.header.frame_id
            value = None
            if topic in camera_topics:
                name = camera_topics[topic]
                if name not in archives:
                    encoding = config['camera'].get('video',{})
                    archives[name] = VideoArchive(Path(video_folder)/(name+'.mp4'),config['camera']['cameras'][name]['fps'],
                                                  encoding.get('qp',18),encoding.get('gop',15))
                if video and msg.format != 'h264': raise ValueError('Only H.264 streamcopy is supported')
                if not video and stamp_ns < start: continue
                value = archives[name].add(msg.data if video else image_from_ros(msg),stamp_ns,message_id,frame_id,rgb=not video)
                if value is None: continue
            if stamp_ns < start: continue
            sample = Sample(stamp_ns/1e9,value,stamp_ns,message_id,topic,receive_ns,frame_id)
            if topic in input_topics:
                if available[topic] == 'sensor_msgs/msg/JointState':
                    sample.value = reorder([n.removeprefix('leader_') for n in msg.name],msg.position,joints)
                elif available[topic] == 'trajectory_msgs/msg/JointTrajectory' and len(msg.points) == 1:
                    sample.value = reorder(msg.joint_names,msg.points[0].positions,joints)
                else:
                    raise ValueError('Unsupported operator input topic: '+topic)
                input_lists[input_topics[topic]].append(sample)
            elif topic in state_lists:
                sample.value = reorder(msg.name,msg.position,joints)
                if len(msg.velocity) == len(msg.name): sample.velocity = np.asarray(msg.velocity)[[msg.name.index(n) for n in joints]]
                if len(msg.effort) == len(msg.name): sample.effort = np.asarray(msg.effort)[[msg.name.index(n) for n in joints]]
                state_lists[topic].append(sample)
            elif topic == spec['action_topic']:
                if len(msg.points) != 1: raise ValueError('Applied action must have exactly one point')
                sample.value = reorder(msg.joint_names,msg.points[0].positions,joints)
                actions.append(sample)
            else:
                cameras[camera_topics[topic]].append(sample)
            retained += len(data) if topic not in camera_topics else 256
            if retained > spec['max_memory_gb']*1e9:
                raise MemoryError('Episode metadata exceeds memory budget')
        chosen = next((t for t in state_candidates if state_lists.get(t)),None)
        if chosen is None: raise ValueError('Missing measured joint states')
        states = Timeline(state_lists[chosen])
        # Preserve reported velocity if available; otherwise explicitly derive it
        # from adjacent measured positions, never from target commands.
        previous = None
        derived_velocity = False
        for sample in states.samples:
            if sample.velocity is None or not np.isfinite(sample.velocity).all():
                if previous is not None:
                    dt = (sample.timestamp_ns-previous.timestamp_ns)/1e9
                    sample.velocity = (np.asarray(sample.value)-previous.value)/dt if dt <= .1 else np.full(len(joints),np.nan)
                derived_velocity = True
            previous = sample
        actions = Timeline(actions)
        cameras = {n:Timeline(samples) for n,samples in cameras.items()}
        operator = {n:Timeline(samples) for n,samples in input_lists.items() if samples}
        sequence_gaps = {}
        for name,timeline in cameras.items():
            for sample in timeline.samples:
                sample.capture_sequence = frame_metadata[name].get(sample.timestamp_ns,{}).get('source_sequence')
            sequences = [s.capture_sequence for s in timeline.samples if s.capture_sequence is not None]
            sequence_gaps[name] = {'source_sequence_available':len(sequences) == len(timeline.samples),
                                   'missing_host_frames':sum(max(0,b-a-1) for a,b in zip(sequences,sequences[1:]))}
        for name,archive in archives.items(): video_info[name] = archive.finalize()
        report = {'source_topics':{'state':chosen,'action':spec['action_topic'],'cameras':{n:t for t,n in camera_topics.items()},'inputs':{n:t for t,n in input_topics.items()}},
                  'source_clock':recording.get('clock',{}),
                  'state_velocity': 'finite_difference_used_for_missing_values' if derived_velocity else recording.get('state_velocity','reported; measurement/derivation unspecified'),
                  'state_effort': 'available only where state_effort_valid is true; no inferred torque',
                  'message_id_basis': 'recorder topic sequence' if indices else 'legacy per-topic bag ordinal',
                  'camera_host_sequence_gaps':sequence_gaps,
                  'timelines': {'state':timeline_report(states),'action':timeline_report(actions),
                                **{'image/'+n:timeline_report(t,config['camera']['cameras'][n]['fps']) for n,t in cameras.items()},
                                **{'input/'+n:timeline_report(t) for n,t in operator.items()}}}
        return metadata, states, actions, cameras, video_info, report, recording, operator
    finally:
        for archive in archives.values(): archive.close()


def convert(folders, output, repo_id, config, include_failed=False):
    output = Path(output).resolve()
    if output.exists(): raise FileExistsError('Dataset output exists; choose a new dataset version')
    if not folders or len({str(Path(f).resolve()) for f in folders}) != len(folders): raise ValueError('Supply unique episode folders')
    output.parent.mkdir(parents=True,exist_ok=True)
    staging = output.parent/(output.name+'.partial-'+uuid.uuid4().hex[:8])
    settings = alignment_settings(config['dataset'])
    pre = ImagePreprocessor(config['observation'])
    source_bags, episodes = [], []
    try:
        writer = VideoDatasetWriter(staging,repo_id,config['dataset']['fps'],config['robot']['joints'],pre,settings)
        for folder in folders:
            episode_folder = writer.episode_folder()
            metadata,states,actions,cameras,videos,report,recording,operator = read_episode(folder,config,episode_folder/'videos',include_failed)
            span_s = max(t.times_ns[-1] for t in [states,actions,*cameras.values()])/1e9-min(t.times_ns[0] for t in [states,actions,*cameras.values()])/1e9
            if span_s*config['dataset']['fps']*4096 > config['dataset']['max_memory_gb']*1e9:
                raise MemoryError('Target timeline exceeds memory budget; check clock discontinuities or split episodes')
            required = list(config['dataset'].get('input_topics',{})) if metadata.get('control_mode_at_start') == 'TELEOP' else []
            rows = list(aligned_samples(states,actions,cameras,config['dataset']['fps'],settings,operator,required))
            report.update(target_frames=len(rows),valid_frames=sum(r['valid'] for r in rows),
                          rejected_frames=sum(not r['valid'] for r in rows),warning_frames=sum(bool(r['warnings']) for r in rows),
                          synchronization=settings,action_semantics='absolute actually submitted joint position; zero-order hold at target+offset')
            report['repeated_image_assignments'] = {name:sum(r['valid'] for r in rows)-len({r['images'][name].value for r in rows if r['valid']}) for name in cameras}
            report['maximum_absolute_time_error_s'] = {name:max((abs(r['errors'][name]) for r in rows if r['errors'][name] is not None),default=None) for name in rows[0]['errors']} if rows else {}
            if report['valid_frames'] < 2:
                raise ValueError(f"Episode {metadata['episode_id']} has only {report['valid_frames']}/{len(rows)} valid target rows; inspect source frequency/gaps and synchronization thresholds")
            writer.save_episode(metadata['episode_id'],metadata['task_id'],rows,videos,report)
            (episode_folder/'source_episode.json').write_text(json.dumps({'episode':metadata,'recording':recording},indent=2))
            from .review import write_review
            write_review(episode_folder,metadata,rows,videos,writer.info['joint_names'])
            episodes.append({'episode_id':metadata['episode_id'],'frames':len(rows),'valid_frames':report['valid_frames'],
                             'warning_frames':report['warning_frames'],'rejected_frames':report['rejected_frames']})
            source_bags.extend({'path':str(p.resolve()),'sha256':sha256_file(p)} for p in Path(folder).glob('raw/*.mcap'))
        writer.finalize()
        provenance = {'dataset_name':repo_id,'dataset_version':output.name,'converter_version':'0.3.0',
                      'dataset_format_version':VIDEO_FORMAT,'robot_hash':config_hash(config['robot']),
                      'calibration_sha256':calibration_hashes(config['robot']),'source_bags':source_bags,'episodes':episodes,
                      'units':'rad','joint_names':config['robot']['joints'],'action_semantics':'absolute actually submitted joint position',
                      'preprocessing_config':{k:v for k,v in config['observation'].items() if not k.startswith('_')},
                      'observation_hash':config_hash(config['observation']),
                      'camera_config_at_conversion':{k:v for k,v in config['camera'].items() if not k.startswith('_')},
                      'synchronization_config':settings,'target_fps':config['dataset']['fps'],
                      'video_operations':{e['episode_id']:{n:v['operation'] for n,v in e['videos'].items()} for e in writer.info['episodes']}}
        (staging/'provenance.json').write_text(json.dumps(provenance,indent=2))
        staging.rename(output)
        return provenance
    except Exception:
        shutil.rmtree(staging,ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description='MCAP to timestamp-indexed MP4 + NPZ Dataset v2; no H.264 re-encoding')
    parser.add_argument('episodes',nargs='*')
    parser.add_argument('--collection',help='Collection session directory or session.json; convert only accepted episodes')
    parser.add_argument('--config')
    parser.add_argument('--output',required=True)
    parser.add_argument('--dataset-name','--repo-id',dest='repo_id',required=True,help='Local dataset name (no upload)')
    parser.add_argument('--include-failed',action='store_true')
    args = parser.parse_args()
    if bool(args.collection) == bool(args.episodes):
        parser.error('Supply episode paths OR --collection')
    collection = None
    if args.collection:
        source = Path(args.collection)
        source = source/'session.json' if source.is_dir() else source
        collection = json.loads(source.read_text())
        if collection.get('format') != 'so101_collection_v1':
            parser.error('Unknown collection format')
        args.episodes = collection['accepted_episodes']
        if not args.episodes:
            parser.error('Collection has no accepted episodes')
        if collection.get('status') in ('starting', 'running', 'recording'):
            parser.error('Finish collection before conversion')
    config_path = args.config or (collection['runtime_config'] if collection else '/workspace/config/runtime/system.yaml')
    result = convert(args.episodes,args.output,args.repo_id,load_system(config_path),args.include_failed)
    print(json.dumps({'episodes':result['episodes'],'dataset':args.output},indent=2))


if __name__ == '__main__': main()
