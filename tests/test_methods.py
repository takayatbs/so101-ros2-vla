import json
from concurrent.futures import Future
from types import SimpleNamespace
import numpy as np
import pytest
torch = pytest.importorskip('torch')
from torch import nn

from so101_core.flow import flow_loss,sample,prefix_weights
from so101_core.async_chunks import AsyncChunkScheduler
from so101_core.method_training import TemporalSamples,split_episodes


class RecordingModel(nn.Module):
    def __init__(self):
        super().__init__();self.values=nn.Parameter(torch.ones(1,6,2));self.config={'chunk_size':6,'joint_names':['a','b']}
        self.times=[];self.inputs=[]
    def encode(self,state,images):return state
    def velocity(self,x,times,context,padding=None):
        self.times.append(times.detach().clone());self.inputs.append(x.detach().clone())
        return self.values.expand_as(x)+x*0


def test_training_rtc_clean_prefix_and_postfix_only_loss():
    model=RecordingModel();actions=torch.full((1,6,2),3.);padding=torch.tensor([[False]*5+[True]])
    loss=flow_loss(model,torch.zeros(1,2),{},actions,padding,torch.tensor([2]),noise=torch.zeros_like(actions),times=torch.tensor([.25]))
    loss.backward()
    torch.testing.assert_close(model.times[0][0,:2],torch.ones(2))
    torch.testing.assert_close(model.inputs[0][0,:2],actions[0,:2])
    assert not model.values.grad[0,:2].any() and not model.values.grad[0,5:].any()
    assert model.values.grad[0,2:5].abs().sum()>0


def test_rtc_uses_soft_masked_gradient_guidance_and_trained_prefix_is_exact():
    model=RecordingModel();model.values.data.zero_()
    previous=torch.full((1,4,2),.25);context=torch.zeros(1,2);noise=torch.zeros(1,6,2)
    normal=sample(model,context,10,noise=noise)
    guided=sample(model,context,10,'rtc',previous,2,noise=noise)
    assert (guided[0,:2]-.25).abs().mean() < (normal[0,:2]-.25).abs().mean()
    assert 0 < guided[0,2].mean() < guided[0,0].mean() and not guided[0,4:].any()
    trained=sample(model,context,10,'training_time_rtc',previous,2,noise=noise)
    torch.testing.assert_close(trained[0,:2],previous[0,:2])
    w=prefix_weights(6,2,4);assert w[:2].eq(1).all() and 0<w[2]<1 and w[4:].eq(0).all()


def test_vlash_shifts_state_and_action_together_but_keeps_image(monkeypatch):
    table={'state':np.arange(8,dtype=np.float32)[:,None], 'action':np.arange(8,dtype=np.float32)[:,None]*10,
           'valid':np.array([True,True,True,True,False,True,True,True])}
    class Frames:
        indices=[(0,1)];tables={0:table};chunk_size=4
        def __len__(self):return 1
        def __getitem__(self,i):return {'images':{'top':np.full((8,8,3),11,dtype=np.uint8)}}
    monkeypatch.setattr(np.random,'randint',lambda *a:2)
    ds=TemporalSamples(Frames(),{'state_mean':[0.],'state_std':[1.],'action_mean':[0.],'action_std':[1.]},'vlash',3)
    f=ds[0]
    assert f['offset']==2 and f['state'].item()==3 and f['action'][0].item()==30
    assert f['images']['top'][0,0,0].item()==pytest.approx(11/255)
    assert f['padding'].tolist()==[False,True,True,True]  # never cross invalid row 4
    fake=SimpleNamespace(info={'episodes':[{'index':0,'source_episode_id':'A'},{'index':1,'source_episode_id':'A'},
                                         {'index':2,'source_episode_id':'B'}]})
    train,val=split_episodes(fake,.5,0)
    assert (0 in train)==(1 in train) and not set(train)&set(val)


class ManualExecutor:
    def submit(self,fn,*args):
        self.args=args;self.future=Future();return self.future
    def shutdown(self,**kw):pass


def engine(method='rtc',correction=None):
    clock=[0.]
    policy=SimpleNamespace(config={'objective':{'rtc':'standard','vlash':'vlash','training_time_rtc':'training_time_rtc'}[method],
        'chunk_size':12,'joint_names':['q'],'max_delay_steps':2,'fps':10},predict=lambda *a,**kw:None)
    scheduler=AsyncChunkScheduler(policy,method,10,3,2,.5,correction,clock=lambda:clock[0])
    scheduler.executor.shutdown();scheduler.executor=ManualExecutor()
    return scheduler,clock,{'observation.state':np.array([0.])}


def test_async_executes_old_chunk_during_inference_and_drops_elapsed_prefix():
    e,clock,obs=engine()
    np.testing.assert_array_equal(e.next(obs),[0])
    e.future.set_result((np.arange(12,dtype=float)[:,None],.1,.1));clock[0]=.1
    np.testing.assert_array_equal(e.next(obs),[1])
    clock[0]=.3;e.next(obs)
    assert e.future is not None
    clock[0]=.4;np.testing.assert_array_equal(e.next(obs),[4])
    e.future.set_result((np.arange(100,112,dtype=float)[:,None],.1,.4));clock[0]=.5
    np.testing.assert_array_equal(e.next(obs),[102])
    e.close()


def test_vlash_rolls_state_from_committed_goals_and_late_mode_result_never_commits():
    e,clock,obs=engine('vlash')
    e.next(obs);e.future.set_result((np.arange(12,dtype=float)[:,None],.1,.1));clock[0]=.3
    e.next(obs)
    submitted,previous,delay=e.executor.args
    assert submitted['observation.state'][0]==4 and previous is None and delay==0
    old=e.future;e.reset();old.set_result((np.full((12,1),999.),.1,.4));clock[0]=.4
    assert e.next(obs)[0]==0 and e.chunk is None
    e.close()


def test_async_latency_overrun_and_exhaustion_are_rejected():
    e,clock,obs=engine();e.next(obs)
    e.future.set_result((np.zeros((12,1)),.1,.1));clock[0]=.3;e.next(obs)
    e.future.set_result((np.zeros((12,1)),.4,.7));clock[0]=.7
    with pytest.raises(ValueError,match='trained delay'):e.next(obs)
    e.close()


def test_abandoned_failure_does_not_poison_new_mode_and_correction_uses_latest_observation():
    class Correction:
        config={'max_delay_steps':2}
        def correct(self,observation,base,position,delay):return base+observation['observation.state']
    e,clock,obs=engine(correction=Correction());e.next(obs)
    old=e.future;e.reset();old.set_exception(RuntimeError('old mode failure'))
    clock[0]=.1
    np.testing.assert_array_equal(e.next(obs),[0])
    e.future.set_result((np.arange(12,dtype=float)[:,None],.1,.2));clock[0]=.2
    obs['observation.state']=np.array([10.]);np.testing.assert_array_equal(e.next(obs),[11.])
    clock[0]=.3;obs['observation.state']=np.array([20.]);np.testing.assert_array_equal(e.next(obs),[22.])
    e.close()
