import sys,json,wave,math,hashlib
from pathlib import Path
items=json.load(sys.stdin)
results=[]
for item in items:
 source_file=Path(item['path']).read_bytes()
 with wave.open(item['path'],'rb') as audio:
  rate=audio.getframerate();channels=audio.getnchannels();frames=audio.getnframes();width=audio.getsampwidth();raw=audio.readframes(frames)
 assert width==3 and channels==1
 values=[int.from_bytes(raw[offset:offset+3],'little',signed=True) for offset in range(0,len(raw),3)]
 scale=2**23
 def stats(start,end):
  chosen=values[start:end];square_sum=sum(value*value for value in chosen);peak=max(abs(value) for value in chosen)
  rms=math.sqrt(square_sum/len(chosen))/scale
  return dict(startFrame=start,endFrameExclusive=end,rmsDbfs=20*math.log10(rms) if rms else None,peakDbfs=20*math.log10(peak/scale) if peak else None)
 block=round(.010*rate);runs=[];begin=None
 for offset in range(0,frames-block+1,block):
  below=sum(value*value for value in values[offset:offset+block])/block <= (10**(-50/10))*scale*scale
  if below and begin is None:begin=offset
  if not below and begin is not None:
   if (offset-begin)/rate>=.5:runs.append([begin,offset])
   begin=None
 if begin is not None:
  end=(frames//block)*block
  if (end-begin)/rate>=.5:runs.append([begin,end])
 result=dict(id=item['id'],sourceSha256=hashlib.sha256(source_file).hexdigest(),sourceBytes=len(source_file),rate=rate,frames=frames,whole=stats(0,frames),quiet=runs)
 if item['pause']:result['region']=stats(item['pause']['startFrame'],item['pause']['endFrameExclusive'])
 results.append(result)
print(json.dumps(dict(method='stdlib wave; signed 24-bit integers; exact integer sum-of-squares before scaling; independent Python quiet-run traversal',pythonRuntime=sys.version,referenceScriptSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),fixtures=results)))
