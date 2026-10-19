"""Private, expiring uploaded sources shared by channel previews and import."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from utils import file_lock

STAGE_SECONDS = 1800
MAX_STAGES = 8
TOKEN = re.compile(r'[0-9a-f]{32}\Z')


class AudioChannelStaging:
    def __init__(self, root):
        self.root = Path(root) / '.audio-channel-previews'

    def get_directory(self, token):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ValueError('Invalid audio preview handle.')
        return self.root / token

    def remove_expired(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in self.root.iterdir():
            if (directory.is_dir() and TOKEN.fullmatch(directory.name)
                    and time.time() - directory.stat().st_mtime > STAGE_SECONDS):
                with file_lock(str(self.root / ('channel-' + directory.name[0])), timeout=100):
                    if directory.exists() and time.time() - directory.stat().st_mtime > STAGE_SECONDS:
                        shutil.rmtree(directory)

    def create_stage(self, filename, purpose):
        if purpose not in ('clone', 'preparer'):
            raise ValueError('Unknown audio preview purpose.')
        self.remove_expired()
        with file_lock(str(self.root / '.create')):
            if sum(p.is_dir() for p in self.root.iterdir()) >= MAX_STAGES:
                raise ValueError('Too many pending audio uploads. Close an earlier preview or wait for expiry.')
            token = uuid.uuid4().hex
            directory = self.root / token
            directory.mkdir(mode=0o700)
            metadata = {'filename': Path(filename).name, 'purpose': purpose, 'created': time.time()}
            (directory / 'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
            return token, directory / ('source' + Path(filename).suffix.lower())

    @contextmanager
    def open_stage(self, token, purpose=None):
        directory = self.get_directory(token)
        if not self.root.is_dir():
            raise ValueError("Audio preview expired or was removed. Select the file again.")
        with file_lock(str(self.root / ('channel-' + directory.name[0])), timeout=100):
            if not directory.is_dir():
                raise ValueError('Audio preview expired or was removed. Select the file again.')
            metadata = json.loads((directory / 'metadata.json').read_text(encoding='utf-8'))
            if time.time() - metadata['created'] > STAGE_SECONDS:
                shutil.rmtree(directory)
                raise ValueError('Audio preview expired. Select the file again.')
            if purpose is not None and metadata['purpose'] != purpose:
                raise ValueError('Audio upload was prepared for a different operation.')
            path = directory / ('source' + Path(metadata['filename']).suffix.lower())
            if not path.is_file():
                raise ValueError('Audio upload has not completed.')
            yield path, metadata

    def ensure_stage_metadata(self, token, purpose):
        with self.open_stage(token, purpose) as (_, metadata):
            return dict(metadata)

    def copy_stage(self, token, destination, purpose):
        with self.open_stage(token, purpose) as (path, metadata):
            shutil.copyfile(path, destination)
            return dict(metadata)

    def remove_stage(self, token):
        directory = self.get_directory(token)
        if not self.root.is_dir():
            return
        with file_lock(str(self.root / ('channel-' + directory.name[0])), timeout=100):
            if directory.exists():
                shutil.rmtree(directory)

    def move_stage(self, token, destination, purpose):
        with self.open_stage(token, purpose) as (path, metadata):
            os.replace(path, destination)
            return dict(metadata)
