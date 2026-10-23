"""Native cover-output checks using generated pixels/audio and no model runtime."""
import argparse
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import wave
import zlib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path, default=Path(__file__).resolve().parents[1]/'app')
    args = parser.parse_args()
    sys.path.insert(0, str(args.app_dir.resolve()))
    from media_process import run_owned_media_check
    from m4b_cover import ensure_m4b_output_cover
    def run(arguments):
        run_owned_media_check(['ffmpeg','-nostdin','-v','error','-y',*arguments],lambda:None,30)
    def png_chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    pixels = b'\x89PNG\r\n\x1a\n'
    pixels += png_chunk(b'IHDR',struct.pack('>IIBBBBB',2,2,8,2,0,0,0))
    pixels += png_chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\0\0'*2)*2))
    pixels += png_chunk(b'IEND',b'')
    passed = 0
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        wav = root/'audio.wav'
        with wave.open(str(wav),'wb') as stream:
            stream.setparams((1,2,24000,24000,'NONE','not compressed'))
            stream.writeframes(b'\x00\x10'*24000)
        png = root/'cover.png';png.write_bytes(pixels)
        jpeg = root/'cover.jpg';run(['-i',str(png),'-frames:v','1',str(jpeg)])
        outputs = {}
        for name, cover in [('none',None),('png',png),('jpeg',jpeg)]:
            output = root/(name+'.m4b')
            command = ['-i',str(wav)]
            if cover is not None:
                command += ['-i',str(cover)]
            command += ['-map','0:a:0','-c:a','aac','-b:a','128k']
            if cover is not None:
                command += ['-map','1:v:0','-c:v','copy','-disposition:v:0','attached_pic']
            run([*command,'-f','mp4',str(output)])
            digest = hashlib.sha256(cover.read_bytes()).hexdigest() if cover is not None else None
            identity = ensure_m4b_output_cover(output,digest)
            if identity['sha256'] != hashlib.sha256(output.read_bytes()).hexdigest():
                raise RuntimeError('Cover verification returned a different file identity')
            outputs[name] = output
            passed += 1
        png_digest = hashlib.sha256(png.read_bytes()).hexdigest()
        for path,digest in [(outputs['none'],png_digest),(outputs['jpeg'],png_digest),(outputs['png'],None)]:
            try:
                ensure_m4b_output_cover(path,digest)
            except ValueError:
                passed += 1
            else:
                raise RuntimeError('Invalid finished cover was accepted')
    if passed != 6:
        raise RuntimeError('Incomplete native cover qualification')
    print('Native cover-output checks: 6 passed, 0 skipped')


if __name__ == '__main__':
    main()
