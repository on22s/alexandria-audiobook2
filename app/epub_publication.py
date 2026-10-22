"""Bounded source suggestions; never apply metadata or write project assets."""
import base64
from collections import Counter
import hashlib
import io
from pathlib import PurePosixPath, PureWindowsPath
import posixpath
import re
import stat
from urllib.parse import unquote, urlsplit
import zipfile

from alexandria_alignment import validate_epub_archive
from epub_package import get_epub_package
from m4b_cover import MAX_COVER_BYTES, get_normalized_cover

MAX_PUBLICATION_XML_BYTES = 2 * 1024**2
MAX_SOURCE_COVER_BYTES = 8 * 1024**2
MAX_FIELD_CHOICES = 32
DC = '{http://purl.org/dc/elements/1.1/}'


def get_epub_cover_member(opf_path, href):
    """Resolve an internal href while refusing external and escaping paths."""
    parsed = urlsplit(href)
    path = unquote(parsed.path)
    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or not path
            or '\\' in path or '\0' in path or path.startswith('/')
            or PureWindowsPath(path).drive):
        raise ValueError('Unsafe EPUB cover reference')
    member = posixpath.normpath(posixpath.join(str(PurePosixPath(opf_path).parent), path))
    if member in ('.', '..') or member.startswith('../'):
        raise ValueError('EPUB cover reference escapes the archive')
    return member


def get_epub_publication_origin(archive_bytes, *, filename, text_sha256):
    """Describe captured bytes; this alone does not certify readable metadata."""
    if not isinstance(filename, str) or not filename or len(filename) > 512:
        raise ValueError('Invalid EPUB origin filename')
    filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
    if not filename or any(ord(char) < 32 for char in filename):
        raise ValueError('Invalid EPUB origin filename')
    if not isinstance(text_sha256, str) or not re.fullmatch('[0-9a-f]{64}', text_sha256):
        raise ValueError('An extracted-text identity is required')
    if not isinstance(archive_bytes, bytes):
        raise ValueError('Captured EPUB must be bytes')
    origin_sha = hashlib.sha256(archive_bytes).hexdigest()
    return {'version': 1, 'id': hashlib.sha256(
                  f'AA2_epub_publication_v1:{origin_sha}:{text_sha256}'.encode()).hexdigest(),
              'origin': {'kind': 'epub', 'filename': filename,
                         'sha256': origin_sha, 'text_sha256': text_sha256},
              'fields': {'title': [], 'author': []}, 'other_creators': [],
              'metadata_status': 'unavailable', 'cover': None,
              'cover_status': 'not_declared', 'warnings': []}


def get_epub_publication_candidate(archive_bytes, *, filename, text_sha256):
    """Capture exact EPUB origin and reviewable options, with no disk writes."""
    validate_epub_archive('', archive_bytes=archive_bytes)
    result = get_epub_publication_origin(archive_bytes, filename=filename, text_sha256=text_sha256)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        opf_path, package = get_epub_package(archive, max_xml_bytes=MAX_PUBLICATION_XML_BYTES,
                                           require_unique=True)
        namespace = package.tag.split('}')[0] + '}' if '}' in package.tag else ''
        if package.tag != namespace + 'package':
            raise ValueError('EPUB root is not a publication package')
        metadata = package.findall(namespace + 'metadata')
        if len(metadata) != 1:
            raise ValueError('EPUB publication metadata is missing or ambiguous')
        metadata = metadata[0]
        result['metadata_status'] = 'available'
        creators = metadata.findall(DC + 'creator')
        ids = Counter(element.get('id') for element in creators if element.get('id'))
        meta_entries = metadata.findall(namespace + 'meta')
        role_entries = {}
        for meta in meta_entries:
            if meta.get('property') == 'role':
                role_entries.setdefault(meta.get('refines'), []).append(meta)
        for element in list(metadata):
            if element.tag not in (DC + 'title', DC + 'creator'):
                continue
            value = ''.join(element.itertext()).strip()
            if not value:
                continue
            if len(value) > 20000:
                raise ValueError('EPUB metadata value exceeds the export limit')
            field = 'title'
            if element.tag == DC + 'creator':
                roles = [element.get('{http://www.idpf.org/2007/opf}role')]
                identifier = element.get('id')
                refinements = role_entries.get('#' + identifier, []) if identifier else []
                roles += [((meta.get('scheme') + ':') if meta.get('scheme') not in (None, 'marc:relators') else '')
                          + ''.join(meta.itertext()).strip() for meta in refinements]
                roles = sorted({role.strip().casefold() for role in roles if role and role.strip()})
                if len(roles) > MAX_FIELD_CHOICES or any(len(role) > 20000 for role in roles):
                    raise ValueError('EPUB has too many or oversized creator roles')
                if (identifier and ids[identifier] != 1) or roles and 'aut' not in roles:
                    result['other_creators'].append({'name': value, 'roles': roles})
                    if len(result['other_creators']) > MAX_FIELD_CHOICES:
                        raise ValueError('EPUB has too many creator choices')
                    continue
                field = 'author'
            if value not in result['fields'][field]:
                result['fields'][field].append(value)
            if len(result['fields'][field]) > MAX_FIELD_CHOICES:
                raise ValueError('EPUB has too many metadata choices')
        if result['other_creators']:
            result['warnings'].append('Other or ambiguous creator roles require manual review.')

        cover_ids = {meta.get('content') for meta in meta_entries
                     if (meta.get('name') or '').casefold() == 'cover' and meta.get('content')}
        items = [item for manifest in package.findall(namespace + 'manifest')
                 for item in manifest.findall(namespace + 'item')]
        selected = [item for item in items if 'cover-image' in (item.get('properties') or '').split()
                    or item.get('id') in cover_ids]
        if len(cover_ids) > 1 or (selected and cover_ids - {item.get('id') for item in selected}):
            result['cover_status'] = 'ambiguous'
            result['warnings'].append('Conflicting cover declarations; upload the chosen cover manually.')
            return result
        if not selected:
            if cover_ids:
                result['cover_status'] = 'unavailable'
                result['warnings'].append('Declared source cover was not found in the manifest.')
            return result
        if (len(selected) != 1 or not selected[0].get('id')
                or sum(item.get('id') == selected[0].get('id') for item in items) != 1):
            result.update(cover_status='ambiguous')
            result['warnings'].append('Multiple or ambiguous covers declared; upload the chosen cover manually.')
            return result
        item = selected[0]
        if item.get('media-type') not in {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}:
            result.update(cover_status='unsupported')
            result['warnings'].append('Source cover format is unsupported; upload a JPEG or PNG cover.')
            return result
        try:
            member = get_epub_cover_member(opf_path, item.get('href') or '')
            matches = [info for info in archive.infolist() if info.filename == member]
            if len(matches) != 1:
                raise ValueError('Cover archive member is missing or ambiguous')
            info = matches[0]
            if (info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1
                    or not 0 < info.file_size <= MAX_COVER_BYTES
                    or info.file_size > max(1024, info.compress_size * 200)):
                raise ValueError('Cover archive member exceeds safe image bounds')
            with archive.open(info) as source:
                raw = source.read(info.file_size + 1)
            if len(raw) != info.file_size:
                raise ValueError('Cover archive member changed')
            cover = get_normalized_cover(raw, max_output_bytes=MAX_SOURCE_COVER_BYTES)
            result['cover'] = {'member': member, 'media_type': cover['media_type'],
                               'sha256': cover['sha256'],
                               'data_base64': base64.b64encode(cover['data']).decode('ascii')}
            result['cover_status'] = 'available'
        except (ValueError, KeyError, OSError, RuntimeError, zipfile.BadZipFile) as error:
            result['cover_status'] = 'unavailable'
            result['warnings'].append('Source cover unavailable: ' + str(error)[:2000])
    return result
