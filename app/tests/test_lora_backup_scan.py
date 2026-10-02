import builtins
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from routers import lora


class LoraBackupScanTests(unittest.TestCase):
    def test_status_counts_current_file_sizes_without_reading_weight_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);backup=root/'voice/promotion_backups/one';backup.mkdir(parents=True)
            manifest=root/'manifest.json';manifest.write_text('[{"id":"voice","promotion":{"backup_id":"one"}}]')
            for name in lora.PROMOTION_FILES:(backup/name).write_bytes(b'one')
            weights=backup/'adapter_model.safetensors'
            real_open=builtins.open
            def no_payload_read(path,*args,**kwargs):
                if Path(path).parent==backup:raise AssertionError('status tried to read backup payload')
                return real_open(path,*args,**kwargs)
            measurements=[]
            for size in (1024**2,8*1024**3):
                with weights.open('r+b') as handle:handle.truncate(size)
                for _ in range(3):
                    seen=[];real_size=lora.os.path.getsize
                    def getsize(path):
                        if Path(path).parent==backup:seen.append(Path(path).name)
                        return real_size(path)
                    start=time.perf_counter()
                    with patch('builtins.open',side_effect=no_payload_read),patch.object(lora.os.path,'getsize',side_effect=getsize):
                        result=lora._get_lora_backup_status(tmp,str(manifest))
                    measurements.append({'logical_weight_bytes':size,'elapsed':time.perf_counter()-start,'backup_stats':len(seen)})
                    self.assertEqual(size+12,result['total_size_bytes'])
                    self.assertEqual(set(lora.PROMOTION_FILES),set(seen))
                    self.assertEqual(5,len(seen))
            (backup/'extra.txt').write_bytes(b'new extra backup data')
            changed=lora._get_lora_backup_status(tmp,str(manifest))
            self.assertEqual(8*1024**3+12+len(b'new extra backup data'),changed['total_size_bytes'])
            print('BACKUP_SCAN_MEASUREMENTS',json.dumps(measurements))
