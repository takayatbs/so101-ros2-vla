"""LeRobotDataset v3.0 export and local reading, without changing the ROS venv.

Official schema: huggingface.co/docs/lerobot/en/lerobot-dataset-v3.
Export uses CFR videos of the selected, preprocessed samples. Source times
remain int64 columns; invalid gaps split episodes rather than compressing time.
"""
import json
import shutil
import uuid
from pathlib import Path
import numpy as np
from .dataset import SO101Dataset
from .video_storage import VideoArchive, VideoReader
from fractions import Fraction


def continuous_runs(valid):
    indices = np.flatnonzero(valid)
    return [r for r in np.split(indices, np.flatnonzero(np.diff(indices) != 1)+1) if len(r) >= 2]


class CFRVideo:
    def __init__(self,path,fps,codec):
        import av
        self.av,self.fps,self.codec=av,fps,codec
        path.parent.mkdir(parents=True,exist_ok=True)
        self.output=av.open(str(path),'w',options={'movflags':'faststart'})
        self.stream=None

    def add(self,image,index):
        if self.stream is None:
            self.stream=self.output.add_stream(self.codec,rate=self.fps)
            self.stream.width,self.stream.height=image.shape[1],image.shape[0]
            self.stream.pix_fmt='yuv420p'
            self.stream.options=({'preset':'p4','tune':'ull','rc':'constqp','qp':'18','bf':'0','delay':'0'}
                                 if self.codec=='h264_nvenc' else {'crf':'18','preset':'fast','bf':'0'})
        frame=self.av.VideoFrame.from_ndarray(image,format='rgb24')
        frame.pts,frame.time_base=index,Fraction(1,self.fps)
        for packet in self.stream.encode(frame):self.output.mux(packet)

    def close(self):
        if self.output is not None:
            if self.stream:
                for packet in self.stream.encode():self.output.mux(packet)
            self.output.close();self.output=None


def export_lerobot(source, output, encoder='h264_nvenc'):
    import pyarrow as pa
    import pyarrow.parquet as pq
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    ds = SO101Dataset(source, chunk_size=1)
    fps = ds.info['fps']
    if int(fps) != fps:
        raise ValueError('LeRobot export requires an integer target FPS')
    fps = int(fps)
    stage = output.with_name(output.name+'.partial-'+uuid.uuid4().hex[:8])
    stage.mkdir(parents=True)
    features = {k: {'dtype': t, 'shape': [1], 'names': None} for k,t in [
        ('timestamp','float32'),('frame_index','int64'),('episode_index','int64'),('index','int64'),('task_index','int64')]}
    for key in ('observation.state', 'action'):
        features[key] = {'dtype':'float32','shape':[len(ds.info['joint_names'])],'names':ds.info['joint_names']}
    for key in ('source_timestamp_ns','source_state_timestamp_ns','source_action_timestamp_ns','source_frame_index'):
        features[key] = {'dtype':'int64','shape':[1],'names':None}
    for name, shape in ds.info['image_shapes'].items():
        features['observation.images.'+name] = {'dtype':'video','shape':shape,'names':['height','width','channels'],
            'info':{'video.height':shape[0],'video.width':shape[1],'video.channels':3,'video.fps':fps,
                    'video.codec':'h264','video.pix_fmt':'yuv420p','video.is_depth_map':False,'has_audio':False}}
        features['source_image_timestamp_ns.'+name] = {'dtype':'int64','shape':[1],'names':None}
    episodes, tasks, mapping, global_index, vectors = [], [], [], 0, {'observation.state':[],'action':[]}
    image_moments = {n:{'sum':np.zeros(3),'square':np.zeros(3),'min':np.ones(3),'max':np.zeros(3),'count':0} for n in ds.info['image_shapes']}
    lookup = {pair:i for i,pair in enumerate(ds.indices)}
    try:
        for original in ds.info['episodes']:
            table = ds.tables[original['index']]
            task = original['task']
            if task not in tasks: tasks.append(task)
            for run in continuous_runs(table.get('valid',np.ones(len(table['state']),dtype=bool))):
                ei, n = len(episodes), len(run)
                chunk, file = divmod(ei,1000)
                rows = {'observation.state':table['state'][run].tolist(),'action':table['action'][run].tolist(),
                    'timestamp':pa.array(np.arange(n,dtype=np.float32)/fps,type=pa.float32()),
                    'frame_index':list(range(n)), 'episode_index':[ei]*n,
                    'index':list(range(global_index,global_index+n)), 'task_index':[tasks.index(task)]*n}
                for key,column in [('source_timestamp_ns','timestamp_ns'),('source_state_timestamp_ns','state_time_ns'),
                                   ('source_action_timestamp_ns','action_time_ns')]:
                    values = table.get(column, np.round(table['timestamp']*1e9).astype(np.int64))
                    rows[key] = [int(values[r]) for r in run]
                rows['source_frame_index'] = [int(r) for r in run]
                ep = {'episode_index':ei,'tasks':[task],'length':n,'dataset_from_index':global_index,
                      'dataset_to_index':global_index+n,'data/chunk_index':chunk,'data/file_index':file,
                      'meta/episodes/chunk_index':0,'meta/episodes/file_index':0}
                for name in ds.info['image_shapes']:
                    key = 'observation.images.'+name
                    path = stage/f'videos/{key}/chunk-{chunk:03d}/file-{file:03d}.mp4'
                    archive = CFRVideo(path,fps,encoder)
                    try:
                        for i,r in enumerate(run):
                            image = ds[lookup[original['index'],int(r)]]['images'][name]
                            pixels=image[::max(1,image.shape[0]//32),::max(1,image.shape[1]//32)].reshape(-1,3)/255.
                            moments=image_moments[name]
                            moments['sum']+=pixels.sum(0);moments['square']+=(pixels*pixels).sum(0)
                            moments['min']=np.minimum(moments['min'],pixels.min(0));moments['max']=np.maximum(moments['max'],pixels.max(0))
                            moments['count']+=len(pixels)
                            archive.add(image,i)
                    finally:
                        archive.close()
                    ep.update({f'videos/{key}/chunk_index':chunk,f'videos/{key}/file_index':file,
                               f'videos/{key}/from_timestamp':0.,f'videos/{key}/to_timestamp':n/fps})
                    values = table.get('image_time_ns/'+name, table.get('timestamp_ns'))
                    if values is None: values = np.round(table['timestamp']*1e9).astype(np.int64)
                    rows['source_image_timestamp_ns.'+name] = [int(values[r]) for r in run]
                for key in ('observation.state','action'):
                    rows[key] = pa.array(rows[key],type=pa.list_(pa.float32(),len(ds.info['joint_names'])))
                    vectors[key].extend(table['state' if key=='observation.state' else 'action'][run])
                path = stage/f'data/chunk-{chunk:03d}/file-{file:03d}.parquet'
                path.parent.mkdir(parents=True,exist_ok=True)
                pq.write_table(pa.table(rows),path,compression='zstd')
                episodes.append(ep)
                mapping.append({'index':ei,'source_episode_id':original['episode_id'],
                                'source_episode_index':original['index'],'source_first_frame':int(run[0]),
                                'frames':n,'task':task})
                global_index += n
        if not episodes: raise ValueError('No continuous valid segments with at least two frames')
        meta = stage/'meta';(meta/'episodes/chunk-000').mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist(episodes),meta/'episodes/chunk-000/file-000.parquet')
        task_table = pa.table({'task_index':list(range(len(tasks))),'task':tasks})
        # pandas index metadata is the standard LeRobot task lookup contract.
        pandas_meta = {'index_columns':['task'],'column_indexes':[],
            'columns':[{'name':'task_index','field_name':'task_index','pandas_type':'int64','numpy_type':'int64','metadata':None},
                       {'name':'task','field_name':'task','pandas_type':'unicode','numpy_type':'object','metadata':None}],
            'creator':{'library':'pyarrow','version':pa.__version__},'pandas_version':'2.2.0'}
        task_table = task_table.replace_schema_metadata({b'pandas':json.dumps(pandas_meta).encode()})
        pq.write_table(task_table,meta/'tasks.parquet')
        info = {'codebase_version':'v3.0','robot_type':'so101','fps':fps,'features':features,
                'total_episodes':len(episodes),'total_frames':global_index,'total_tasks':len(tasks),
                'chunks_size':1000,'data_files_size_in_mb':100,'video_files_size_in_mb':200,
                'data_path':'data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet',
                'video_path':'videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4',
                'splits':{'train':f'0:{len(episodes)}'}}
        stats = {}
        for key, values in vectors.items():
            a = np.asarray(values)
            stats[key] = {k:v.tolist() for k,v in [('min',a.min(0)),('max',a.max(0)),('mean',a.mean(0)),
                                                ('std',a.std(0)),('count',np.array([len(a)]))]}
        for name in ds.info['image_shapes']:
            m=image_moments[name];mean=m['sum']/m['count']
            std=np.sqrt(np.maximum(0,m['square']/m['count']-mean*mean))
            stats['observation.images.'+name] = {k:v[:,None,None].tolist() for k,v in [('min',m['min']),('max',m['max']),('mean',mean),('std',std)]}
            stats['observation.images.'+name]['count']=[global_index]
        (meta/'info.json').write_text(json.dumps(info,indent=2))
        (meta/'stats.json').write_text(json.dumps(stats,indent=2))
        provenance = json.loads((source/'provenance.json').read_text())
        provenance.update(source_dataset_format=provenance.get('dataset_format_version'),
                          dataset_format_version='lerobot_v3.0',dataset_version=output.name)
        (stage/'provenance.json').write_text(json.dumps(provenance,indent=2))
        bridge = {'format':'so101_lerobot_bridge_v1','source_dataset':str(source),'units':'rad',
                  'action_semantics':'absolute joint position actually submitted',
                  'image_shapes':ds.info['image_shapes'],'joint_names':ds.info['joint_names'],
                  'episodes':mapping,'fps':fps,'synchronization':ds.info.get('synchronization',{}),
                  'video_operation':encoder+' offline CFR resampling of aligned, preprocessed images',
                  'split_rule':'group all segments of the same source_episode_id together',
                  'image_statistics':'sampled RGB channel moments over exported images; use train-only statistics when holding out episodes'}
        (stage/'so101_bridge.json').write_text(json.dumps(bridge,indent=2))
        stage.rename(output)
    except Exception:
        shutil.rmtree(stage,ignore_errors=True)
        raise
    return output


class LeRobotFrames:
    """Read exported Parquet/MP4 without imposing LeRobot's deps on ROS."""
    def __init__(self, root, chunk_size=30, episode_indices=None):
        import pyarrow.parquet as pq
        self.root, self.chunk_size = Path(root), chunk_size
        bridge = json.loads((self.root/'so101_bridge.json').read_text())
        if bridge['format'] != 'so101_lerobot_bridge_v1' or bridge['units'] != 'rad':
            raise ValueError('Export with this project to establish units/calibration provenance')
        self.info = {**bridge,'format':'lerobot_v3.0'}
        selected = set(episode_indices) if episode_indices is not None else {e['index'] for e in bridge['episodes']}
        self.tables, self.indices, self.reader = {}, [], VideoReader()
        for episode in bridge['episodes']:
            ei = episode['index']
            if ei not in selected: continue
            chunk,file = divmod(ei,1000)
            rows = pq.read_table(self.root/f'data/chunk-{chunk:03d}/file-{file:03d}.parquet').to_pydict()
            a = {'state':np.asarray(rows['observation.state'],dtype=np.float32),
                 'action':np.asarray(rows['action'],dtype=np.float32),
                 'timestamp':np.asarray(rows['timestamp'],dtype=np.float64),
                 'timestamp_ns':np.asarray(rows['source_timestamp_ns'],dtype=np.int64).ravel()}
            a['valid'] = np.ones(len(a['state']),dtype=bool)
            self.tables[ei] = a
            self.indices.extend((ei,i) for i in range(len(a['state'])))

    def __len__(self): return len(self.indices)

    def __getitem__(self, index):
        ei,fi = self.indices[index];table = self.tables[ei];n = len(table['state'])
        chunk,file = divmod(ei,1000)
        images = {name:self.reader.frame(self.root/f'videos/observation.images.{name}/chunk-{chunk:03d}/file-{file:03d}.mp4',
                    round(fi*1e9/self.info['fps'])) for name in self.info['image_shapes']}
        return {'state':table['state'][fi].copy(),'action':table['action'][np.minimum(np.arange(fi,fi+self.chunk_size),n-1)].copy(),
                'padding':np.arange(fi,fi+self.chunk_size)>=n,'images':images,'timestamp':table['timestamp'][fi]}

    def statistics(self):
        return {key+'_'+kind:func(np.concatenate([t[key] for t in self.tables.values()]),axis=0).clip(.01,None).tolist()
                if kind=='std' else func(np.concatenate([t[key] for t in self.tables.values()]),axis=0).tolist()
                for key in ('state','action') for kind,func in [('mean',np.mean),('std',np.std)]}


def open_frames(root, chunk_size=30, episode_indices=None):
    return (LeRobotFrames if (Path(root)/'meta/info.json').exists() else SO101Dataset)(root,chunk_size,episode_indices)
