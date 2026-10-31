import json
from pathlib import Path
import shutil

import numpy as np
import pytest
import yaml
from so101_core.config import load_system, config_hash, calibration_hashes
from so101_core.dataset import SO101Dataset, VideoDatasetWriter, VIDEO_FORMAT
from so101_core.preprocessing import ImagePreprocessor
from so101_core.synchronization import Sample, Timeline, aligned_samples, alignment_settings
from so101_core.video import H264Encoder, H264Decoder
from so101_core.video_storage import VideoArchive, VideoReader


def make_video_dataset(tmp_path):
    source = Path(__file__).resolve().parents[1]
    config_folder = tmp_path/'config'
    shutil.copytree(source/'config',config_folder)
    path = config_folder/'observation/observation.yaml'
    spec = yaml.safe_load(path.read_text())
    for pre in spec['preprocessing'].values(): pre['resize'].update(width=64,height=48)
    path.write_text(yaml.safe_dump(spec))
    config = load_system(config_folder/'runtime/system.real30hz.640x480.h264.yaml')
    output = tmp_path/'dataset'
    settings = alignment_settings(config['dataset'])
    writer = VideoDatasetWriter(output,'test/video-v2',30,config['robot']['joints'],ImagePreprocessor(config['observation']),settings)
    expected = {}
    for episode in range(2):
        origin = 1_792_900_000_000_000_003+episode*10_000_000_000
        cameras, videos = {}, {}
        for name in spec['cameras']:
            archive = VideoArchive(writer.episode_folder()/f'videos/{name}.mp4',30)
            encoder, decoder = H264Encoder(160,120,30), H264Decoder()
            samples = []
            try:
                for i in range(35):
                    # A real capture gap occurs BEFORE encoding: later delta
                    # frames retain their complete reference chain.
                    if 10 <= i <= 15: continue
                    rgb = np.full((120,160,3),[40+i*3,80,180-i*2],dtype=np.uint8)
                    packet = encoder.encode(rgb)
                    decoded = decoder.decode(packet)
                    timestamp = origin+round(i*1e9/30)
                    index = archive.add(packet,timestamp,i,'camera_'+name)
                    expected[episode,name,index] = decoded
                    samples.append(Sample(timestamp/1e9,index,timestamp,i,f'/camera/{name}/video'))
                videos[name] = archive.finalize()
            finally:
                encoder.close();archive.close()
            cameras[name] = Timeline(samples)
        state = Timeline([Sample((origin+i*10_000_000)/1e9,np.array([episode+i*.001]*6),origin+i*10_000_000,i,'/state') for i in range(115)])
        action = Timeline([Sample(s.time,s.value+.1,s.timestamp_ns,i,'/action') for i,s in enumerate(state.samples)])
        rows = list(aligned_samples(state,action,cameras,30,settings))
        assert any(not r['valid'] for r in rows)
        writer.save_episode(f'ep-{episode}','synthetic video task',rows,videos,{'test':True})
    writer.finalize()
    provenance = {'dataset_name':'test/video-v2','dataset_version':'v2','dataset_format_version':VIDEO_FORMAT,
                  'observation_hash':config_hash(config['observation']),'robot_hash':config_hash(config['robot']),
                  'calibration_sha256':calibration_hashes(config['robot']),'units':'rad','joint_names':config['robot']['joints']}
    (output/'provenance.json').write_text(json.dumps(provenance))
    return config, output, expected


def test_mp4_preserves_decoded_pixels_timestamps_random_access_and_gap_boundaries(tmp_path):
    pytest.importorskip('av')
    config, output, expected = make_video_dataset(tmp_path)
    dataset = SO101Dataset(output,chunk_size=30)
    assert not list(output.rglob('*.png'))
    assert len(list(output.rglob('*.mp4'))) == 4
    assert len(dataset) < sum(e['frames'] for e in dataset.info['episodes'])
    pre = ImagePreprocessor(config['observation'])
    # Backward seeks, forward seeks, different episodes and different GOPs.
    for index in [len(dataset)-1,0,19,2,25,5,len(dataset)//2]:
        frame = dataset[index]
        episode,row = dataset.indices[index]
        table = dataset.tables[episode]
        for name in config['observation']['cameras']:
            source = int(table['image_index/'+name][row])
            np.testing.assert_array_equal(frame['images'][name],pre(name,expected[episode,name,source]))
        assert frame['images']['top'].shape == (48,64,3)
        valid_count = int((~frame['padding']).sum())
        assert table['valid'][row:row+valid_count].all()
        if row+valid_count < len(table['valid']): assert not table['valid'][row+valid_count]
        tail = frame['action'][valid_count-1:]
        np.testing.assert_allclose(tail,np.broadcast_to(frame['action'][valid_count-1],tail.shape))
    assert dataset.video_reader.used <= 64*1024*1024
    assert len(dataset.video_reader.opened) <= 4
    valid_states = np.concatenate([t['state'][t['valid']] for t in dataset.tables.values()])
    np.testing.assert_allclose(dataset.statistics()['state_mean'],valid_states.mean(axis=0),atol=1e-6)


def test_mp4_long_capture_gaps_keep_exact_source_times_and_seekable_frames(tmp_path):
    import av
    encoder,decoder = H264Encoder(160,120,30),H264Decoder()
    archive = VideoArchive(tmp_path/'long-gaps.mp4',30)
    stamps = [1_792_900_000_000_000_003,1_792_900_000_100_000_006,
              1_792_900_003_500_000_014,1_792_900_014_100_000_040]
    expected=[]
    try:
        for i,stamp in enumerate(stamps):
            packet=encoder.encode(np.full((120,160,3),[40+i*30,90,150],dtype=np.uint8))
            expected.append(decoder.decode(packet))
            archive.add(packet,stamp,i)
        archive.finalize()
    finally:
        encoder.close();archive.close()
    with np.load(tmp_path/'long-gaps.npz') as index:
        np.testing.assert_array_equal(index['timestamp_ns'],stamps)
        reader=VideoReader()
        for i in (3,0,2,1):
            np.testing.assert_array_equal(reader.frame(tmp_path/'long-gaps.mp4',int(index['pts_ns'][i])),expected[i])
        reader.close()


@pytest.mark.ml
@pytest.mark.skipif(__import__('os').environ.get('RUN_ML_TESTS') != '1',reason='Set RUN_ML_TESTS=1')
def test_training_from_video_registers_v2_and_uses_worker_local_decoders(tmp_path):
    import subprocess,sys,os
    config,output,_ = make_video_dataset(tmp_path)
    training = tmp_path/'training'
    command = [sys.executable,str(Path(__file__).resolve().parents[1]/'training/train.py'),
               '--config',str(tmp_path/'config/runtime/system.real30hz.640x480.h264.yaml'),
               '--dataset',str(output),'--output',str(training),'--model-id','video-v2-smoke',
               '--steps','2','--batch-size','2','--device','cpu','--dim-model','32','--n-heads','4',
               '--dim-feedforward','64','--n-encoder-layers','1','--n-decoder-layers','1','--n-vae-encoder-layers','1','--num-workers','2']
    result = subprocess.run(command,capture_output=True,text=True,timeout=120,
                            env=dict(os.environ,OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2'))
    assert result.returncode == 0,result.stdout+result.stderr
    manifest = json.loads((tmp_path/'data/models/video-v2-smoke/deployment.json').read_text())
    assert manifest['dataset_format'] == VIDEO_FORMAT
    assert np.isfinite(manifest['metrics']['validation_l1_rad'])
