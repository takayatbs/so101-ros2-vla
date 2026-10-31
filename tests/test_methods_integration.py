"""Train real tiny networks on synthetic data. Never connect to USB hardware."""
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import time
import numpy as np
import pytest
torch = pytest.importorskip('torch')
import yaml
from so101_core.config import load_system,config_hash,calibration_hashes
from so101_core.dataset import DatasetWriter,FORMAT
from so101_core.lerobot_io import export_lerobot,open_frames
from so101_core.method_training import train_flow,prepare_correction,train_correction,evaluate_model
from so101_core.flow import FlowPolicy,A2C2
from so101_core.synchronization import alignment_settings

pytestmark = [pytest.mark.ml, pytest.mark.skipif(os.environ.get('RUN_ML_TESTS') != '1', reason='Set RUN_ML_TESTS=1')]


@pytest.fixture(scope='module')
def trained_methods(tmp_path_factory):
    torch.set_num_threads(2)
    root=tmp_path_factory.mktemp('methods')
    config=root/'config';config.mkdir()
    source=Path(__file__).resolve().parents[1]/'config'
    shutil.copytree(source,config,dirs_exist_ok=True)
    p=config/'observation/observation.yaml';obs=yaml.safe_load(p.read_text())
    for spec in obs['preprocessing'].values():spec['resize'].update(width=64,height=64)
    p.write_text(yaml.safe_dump(obs))
    for p in config.glob('policy/policy.*.yaml'):
        v=yaml.safe_load(p.read_text());v['device']='cpu';p.write_text(yaml.safe_dump(v))
    cfg=load_system(config/'runtime/system.real30hz.640x480.rtc.yaml')
    native=root/'native'
    writer=DatasetWriter(native,'local/methods',30,cfg['robot']['joints'],{n:[64,64,3] for n in obs['cameras']})
    for ep in range(3):
        for i in range(10):
            q=np.full(6,i*.01+ep*.02,dtype=np.float32)
            writer.add_frame(q,q+.01,{n:np.full((64,64,3),30+i*5,dtype=np.uint8) for n in obs['cameras']},ep+i/30,'move')
        writer.save_episode('synthetic-'+str(ep))
    writer.finalize()
    (native/'provenance.json').write_text(json.dumps({'dataset_format_version':FORMAT,'dataset_version':'native',
        'observation_hash':config_hash(cfg['observation']),'robot_hash':config_hash(cfg['robot']),
        'calibration_sha256':calibration_hashes(cfg['robot']),'units':'rad','joint_names':cfg['robot']['joints'],
        'synchronization_config':alignment_settings(cfg['dataset'])}))
    exported=export_lerobot(native,root/'lerobot',encoder='libx264')
    models={}
    for method in ('rtc','training_time_rtc','vlash'):
        args=SimpleNamespace(dataset=str(exported),config=str(config/f'runtime/system.real30hz.640x480.{method}.yaml'),
            method=method,steps=3,batch_size=2,dim_model=16,n_heads=2,n_layers=1,denoise_steps=4,
            device='cpu',seed=42,output=str(root/('train-'+method)),model_id=method+'-test')
        models[method]=train_flow(args)
    cache=prepare_correction(SimpleNamespace(model=str(models['rtc']),dataset=str(exported),device='cpu',
        output=str(root/'cache'),seed=42,stride=3,max_samples=10000))
    correction=train_correction(SimpleNamespace(cache=str(cache),output=str(root/'correction'),device='cpu',
        steps=3,batch_size=8,hidden=16,seed=42))
    vlash_cache=prepare_correction(SimpleNamespace(model=str(models['vlash']),dataset=str(exported),device='cpu',
        output=str(root/'cache-vlash'),seed=42,stride=3,max_samples=10000))
    train_correction(SimpleNamespace(cache=str(vlash_cache),output=str(root/'correction-vlash'),device='cpu',
        steps=3,batch_size=8,hidden=16,seed=42))
    trained_rtc_cache=prepare_correction(SimpleNamespace(model=str(models['training_time_rtc']),dataset=str(exported),
        device='cpu',output=str(root/'cache-training_time_rtc'),seed=42,stride=3,max_samples=10000))
    train_correction(SimpleNamespace(cache=str(trained_rtc_cache),output=str(root/'correction-training_time_rtc'),
        device='cpu',steps=3,batch_size=8,hidden=16,seed=42))
    return root,config,native,exported,models,correction


def test_le_robot_export_training_all_methods_and_correction_integrity(trained_methods):
    root,config,native,exported,models,correction=trained_methods
    info=json.loads((exported/'meta/info.json').read_text());assert info['codebase_version']=='v3.0'
    ds=open_frames(exported,30)
    assert len(ds)==30
    for index in [0,1,9,10,29]:assert ds[index]['images']['top'].shape==(64,64,3)
    for method,model in models.items():
        p=FlowPolicy(model/'checkpoint','cpu');f=ds[0]
        observation={'observation.state':f['state'],**{'observation.images.'+n:v for n,v in f['images'].items()}}
        prefix=np.tile(f['action'][0],(8,1))
        output=p.predict(observation,method,prefix,2)
        assert output.shape==(30,6) and np.isfinite(output).all()
    p=FlowPolicy(models['rtc']/'checkpoint','cpu');head=A2C2(correction,p)
    corrected=head.correct(observation,output[0],2,1)
    assert corrected.shape==(6,) and np.isfinite(corrected).all()
    with pytest.raises(ValueError,match='different base'):
        A2C2(correction,FlowPolicy(models['vlash']/'checkpoint','cpu'))


@pytest.mark.ros
@pytest.mark.parametrize('method', ['rtc','training_time_rtc','vlash','rtc+a2c2','vlash+a2c2',
    'rtc+smooth','training_time_rtc+smooth','vlash+smooth',
    'rtc+a2c2+smooth','vlash+a2c2+smooth','training_time_rtc+a2c2+smooth'])
def test_ros_policy_warmup_async_output_and_mode_stop(trained_methods,monkeypatch,method):
    rclpy=pytest.importorskip('rclpy')
    from rclpy.executors import SingleThreadedExecutor
    from sensor_msgs.msg import Image,JointState
    from std_msgs.msg import String,Bool
    from trajectory_msgs.msg import JointTrajectory
    from so101_core.ros_utils import STATE_QOS,COMMAND_QOS,LATCHED_QOS,joint_state
    from so101_policy.node import PolicyNode
    root,folder,native,exported,models,correction=trained_methods
    parts=method.split('+'); base=parts[0]
    if base=='vlash':correction=root/'correction-vlash'
    if base=='training_time_rtc':correction=root/'correction-training_time_rtc'
    profile=base+'.a2c2' if 'a2c2' in parts else base
    monkeypatch.setenv('ROS_DOMAIN_ID','97')
    rclpy.init(args=['--ros-args','-p',f'config:={folder}/runtime/system.real30hz.640x480.{profile}.yaml',
                    '-p','policy_backend:=flow','-p',f'model_id:={base}-test','-p',f'correction_checkpoint:={correction}',
                    '-p','action_smoothing:='+('three_point' if method.endswith('+smooth') else 'none')])
    policy=PolicyNode();peer=rclpy.create_node('method_test_peer');executor=SingleThreadedExecutor()
    executor.add_node(policy);executor.add_node(peer)
    ready=[];actions=[]
    peer.create_subscription(Bool,'/policy/ready',lambda m:ready.append(m.data),LATCHED_QOS)
    peer.create_subscription(JointTrajectory,'/policy/action',lambda m:actions.append(m),COMMAND_QOS)
    states=peer.create_publisher(JointState,'/hardware/follower/state',STATE_QOS)
    mode=peer.create_publisher(String,'/control_mode',LATCHED_QOS)
    images={n:peer.create_publisher(Image,'/camera/'+n+'/image_raw',STATE_QOS) for n in ['top','arm']}
    def publish():
        msg=joint_state(peer,policy.joints,[.02]*6);states.publish(msg)
        for name,pub in images.items():
            image=Image(height=64,width=64,encoding='rgb8',step=192,data=bytes([50])*(64*64*3))
            image.header=msg.header;pub.publish(image)
    timer=peer.create_timer(1/30,publish)
    def until(predicate,timeout=8):
        end=time.monotonic()+timeout
        while not predicate() and time.monotonic()<end:executor.spin_once(timeout_sec=.01)
        assert predicate()
    try:
        mode.publish(String(data='IDLE'));until(lambda:bool(ready) and ready[-1])
        assert not actions
        mode.publish(String(data='AUTONOMOUS'));until(lambda:len(actions)>=15)
        assert all(np.isfinite(m.points[0].positions).all() for m in actions)
        if 'smooth' in parts:
            assert policy.engine.smoothing == 'three_point'
            assert np.isfinite(policy.engine.filtered_roughness_rad)
            assert policy.engine.filtered_roughness_rad >= 0
        if method=='rtc':
            policy.engine.max_inference=.000001
            until(lambda:policy.engine_failed)
            count=len(actions);end=time.monotonic()+.2
            while time.monotonic()<end:executor.spin_once(timeout_sec=.01)
            assert len(actions)==count  # failure stays latched; no bootstrap retry commands
        mode.publish(String(data='TELEOP'));until(lambda:policy.mode=='TELEOP')
        count=len(actions);end=time.monotonic()+.3
        while time.monotonic()<end:executor.spin_once(timeout_sec=.01)
        assert len(actions)==count
    finally:
        peer.destroy_timer(timer);executor.shutdown();policy.destroy_node();peer.destroy_node();rclpy.shutdown()


@pytest.mark.parametrize('method,with_correction', [('rtc', False), ('training_time_rtc', False),
                                                  ('vlash', False), ('rtc', True), ('vlash', True)])
def test_offline_evaluation_records_matching_smoothing_configuration(trained_methods, tmp_path, method, with_correction):
    root,config,native,exported,models,correction=trained_methods
    if method=='vlash':correction=root/'correction-vlash'
    value=evaluate_model(SimpleNamespace(model=str(models[method]),dataset=str(exported),device='cpu',
        samples=2,delay_steps=2,correction=str(correction) if with_correction else None,
        smoothing='three_point',output=str(tmp_path/'evaluation.json')))
    assert value['action_smoothing']=='three_point'
    assert value['smoothing_application']==('corrected_commands_causal' if with_correction else 'predicted_plan')
    assert np.isfinite(value['mean_absolute_error_rad'])


def test_export_preserves_gaps_as_episode_boundaries(trained_methods,tmp_path):
    root,config,native,exported,models,correction=trained_methods
    source=tmp_path/'gapped';shutil.copytree(native,source)
    path=source/'episodes/000000/frames.npz'
    with np.load(path,allow_pickle=False) as f:arrays={k:f[k] for k in f.files}
    arrays['valid']=np.ones(10,dtype=bool);arrays['valid'][3]=False
    np.savez_compressed(path,**arrays)
    destination=export_lerobot(source,tmp_path/'export',encoder='libx264')
    d=open_frames(destination,30)
    segments=[e for e in d.info['episodes'] if e['source_episode_id']=='synthetic-0']
    assert [(e['source_first_frame'],e['frames']) for e in segments]==[(0,3),(4,6)]
    assert len(d)==29
