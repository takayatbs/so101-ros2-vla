"""Relocating configuration must preserve calibration, provenance and old records."""
from pathlib import Path
import copy
import pytest
import yaml
from so101_core.config import load_config, load_system, resolve, config_hash, config_path
from so101_core.collection import CollectionSettings

ROOT = Path(__file__).resolve().parents[1]/'config'


@pytest.fixture(autouse=True)
def public_profiles(monkeypatch):
    monkeypatch.setenv('SO101_USE_LOCAL_CONFIG', '0')


def test_all_profiles_and_legacy_paths_resolve_the_same_resources():
    for path in (ROOT/'runtime').glob('*.yaml'):
        current = load_system(path)
        legacy = load_system(ROOT/path.name)
        assert {k:config_hash(v) for k,v in current.items()} == {k:config_hash(v) for k,v in legacy.items()}
        assert Path(current['controllers']['_file']) == ROOT/'hardware/controllers.yaml'
        assert resolve(current['robot'],current['robot']['leader']['calibration']).resolve() == ROOT/'calibration/leader.json'
        assert resolve(current['dataset'],current['dataset']['bag_root']).resolve() == ROOT.parent/'data/bags'
        assert resolve(current['policy'],current['policy']['model_root']).resolve() == ROOT.parent/'data/models'
    assert config_path(ROOT/'robot.yaml') == ROOT/'hardware/robot.yaml'
    with pytest.raises(FileNotFoundError):load_system(ROOT/'system.does-not-exist.yaml')


def test_resource_path_relocation_preserves_hash_but_changes_are_detected(tmp_path):
    flat = tmp_path/'flat';flat.mkdir()
    structured = tmp_path/'config';(structured/'runtime').mkdir(parents=True)
    (structured/'hardware').mkdir()
    original = {'leader':{'calibration':'calibration/leader.json'},'rate_hz':60}
    (flat/'robot.yaml').write_text(yaml.safe_dump(original))
    moved = copy.deepcopy(original);moved['leader']['calibration']='../calibration/leader.json'
    path = structured/'hardware/robot.yaml';path.write_text(yaml.safe_dump(moved))
    before,after=load_config(flat/'robot.yaml'),load_config(path)
    assert config_hash(before)==config_hash(after)
    # Check the pre-migration digest contract, not only two calls to the new code.
    import hashlib,json
    expected=hashlib.sha256(json.dumps(original,sort_keys=True).encode()).hexdigest()
    assert config_hash(after)==expected
    after['rate_hz']=30
    assert config_hash(after)!=expected
    after['rate_hz']=60;after['leader']['calibration']='../calibration/another.json'
    assert config_hash(after)!=expected


def test_collection_defaults_and_legacy_command_keep_the_same_session_root():
    current=CollectionSettings.from_file(ROOT/'collection/record.yaml')
    legacy=CollectionSettings.from_file(ROOT/'record.yaml')
    assert current==legacy
    assert current.runtime_config==ROOT/'runtime/system.real30hz.640x480.yaml'
    assert current.session_root==ROOT.parent/'data/collections'
