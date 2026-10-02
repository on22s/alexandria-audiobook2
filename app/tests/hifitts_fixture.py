"""Small producer-shaped evaluation with decodable PCM and real PEFT tensors."""
import json
from pathlib import Path
import numpy as np
import soundfile as sf
from tests.test_support import write_test_adapter


def write_hifitts_fixture(root,corpus,work):
    corpus.mkdir(parents=True,exist_ok=True);work.mkdir(parents=True,exist_ok=True)
    waves=corpus/'wavs';waves.mkdir(exist_ok=True)
    books=('trainbook','antoinetteromances4','celebratedcrimesv1')
    rows=[{'id':b+'-001','book':b,'text':'A source sentence. '*4,'normalized':'A source sentence. '*4} for b in books]
    audio=np.sin(np.arange(96000)*.03).astype(np.float32)*.1
    for row in rows:sf.write(waves/(row['id']+'.wav'),audio,24000)
    (corpus/'corpus.json').write_text(json.dumps({'corpus':'fixture','licence':'fixture','sample_rate_native':24000}))
    (corpus/'metadata.csv').write_text('\n'.join(r['id']+'|'+r['text']+'|'+r['normalized'] for r in rows))
    split={'root':str(corpus.relative_to(root)),'train':rows[:1],'test':rows[1:],'test_books':list(books[1:]),'sample_rate_native':24000,'corpus':'fixture','licence':'fixture'}
    (corpus/'split.json').write_text(json.dumps(split))
    train=work/'train';train.mkdir(exist_ok=True);human=work/'human';human.mkdir(exist_ok=True)
    sf.write(train/(rows[0]['id']+'.wav'),audio,24000)
    (train/'metadata.jsonl').write_text(json.dumps({'audio_filepath':rows[0]['id']+'.wav','text':rows[0]['normalized']})+'\n')
    for path in (work/'ref_sample.wav',train/'ref.wav'):sf.write(path,audio,24000)
    (train/'ref_text.txt').write_text(rows[0]['normalized'])
    for r in rows[1:]:sf.write(human/(r['id']+'.wav'),audio,24000)
    build={'native_rate':24000,'target_rate':24000,'train_dir':str(train.relative_to(root)),'metadata':str((train/'metadata.jsonl').relative_to(root)),
        'train':[{'id':rows[0]['id'],'seconds':4}],'test':[{'id':r['id'],'book':r['book'],'text':r['normalized'],'human_wav':str((human/(r['id']+'.wav')).relative_to(root)),'seconds':4} for r in rows[1:]],
        'ref_source_id':rows[0]['id'],'ref_sample':str((work/'ref_sample.wav').relative_to(root)),'ref_text':rows[0]['normalized']}
    (work/'build.json').write_text(json.dumps(build));write_test_adapter(work/'adapter')
    return split,build
