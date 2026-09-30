import importlib.util,json,sys,tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import numpy as np,soundfile as sf
repo=Path(__file__).resolve().parents[3]
spec=importlib.util.spec_from_file_location('campaign',repo/'app/experiments/thunder_audio_campaign.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Engine:
    def __init__(self,c):pass
    def set_sub_batch_size(self,w):pass
    def generate_batch(self,chunks,voices,folder,batch_seed):
        for c in chunks:
            sf.write(str(Path(folder)/f"temp_batch_{c['index']}.wav"),np.sin(np.arange(48000)/20),24000)
        return {'completed':[c['index'] for c in chunks],'failed':[]}
with tempfile.TemporaryDirectory() as folder:
    root=Path(folder);inp=root/'input';inp.mkdir();(inp/'workload.json').write_text(json.dumps({'files':[],'workers':1,'batches':[{'label':'known','voices':{},'chunks':[{'index':0,'speaker':'S','text':'one'},{'index':1,'speaker':'S','text':'two'}]}]}))
    def run(name,engine):
        out=root/name/'result.json'
        with patch.object(sys,'argv',['verify','--inputs',str(inp),'--out',str(out)]),patch.dict(sys.modules,{'tts':SimpleNamespace(TTSEngine=engine)}),patch.object(m.time,'monotonic',side_effect=[0,6]):
            m.main()
        return json.loads(out.read_text())
    result=run('accepted',Engine);assert result['status']=='complete' and result['completed']==2 and result['median_batch_ratio']==1.5
    class Incomplete(Engine):
        def generate_batch(self,*args,**kwargs):return {'completed':[0],'failed':[]}
    try:run('rejected',Incomplete)
    except RuntimeError:pass
    else:raise AssertionError('incomplete generation accepted')
    assert json.loads((root/'rejected'/'result.json').read_text())['status']=='failed'
    # Silent/invalid waveforms cannot become plausible duration measurements.
    sf.write(str(root/'silent.wav'),np.zeros(24000),24000)
    try:m.get_audio_record(root/'silent.wav')
    except ValueError:pass
    else:raise AssertionError('silence accepted')
print('Three known cases passed: exact timing ratio, incomplete-run rejection, silence rejection.')
