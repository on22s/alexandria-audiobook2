"""Shared container/OPF parsing for text extraction and publication review."""
from pathlib import PurePosixPath, PureWindowsPath
import xml.etree.ElementTree as ET


def get_epub_package(archive, *, max_xml_bytes=None, require_unique=False):
    """Read an EPUB package without extracting archive members.

    Text extraction keeps its existing archive-wide admission and first-rootfile
    behavior. Publication review can additionally require bounded XML and an
    unambiguous, confined package. Neither mode follows external references.
    """
    if max_xml_bytes is not None and (type(max_xml_bytes) is not int or max_xml_bytes < 1):
        raise ValueError('EPUB XML limit must be a positive integer')

    def read_member(name):
        if not isinstance(name, str) or not name:
            raise ValueError('Invalid EPUB package member')
        if require_unique:
            path = PurePosixPath(name)
            if ('\\' in name or '\0' in name or path.is_absolute()
                    or PureWindowsPath(name).drive or '..' in path.parts
                    or name != path.as_posix()):
                raise ValueError('Unsafe EPUB package member')
            if sum(info.filename == name for info in archive.infolist()) != 1:
                raise ValueError('EPUB package member is missing or ambiguous')
        info = archive.getinfo(name)
        if max_xml_bytes is None:
            return archive.read(info)
        if info.is_dir() or info.file_size > max_xml_bytes:
            raise ValueError('EPUB package XML exceeds the review limit')
        with archive.open(info) as source:
            raw = source.read(max_xml_bytes + 1)
        if len(raw) > max_xml_bytes or len(raw) != info.file_size:
            raise ValueError('EPUB package XML exceeds the review limit or changed')
        return raw

    container = ET.fromstring(read_member('META-INF/container.xml'))
    ns = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container'}
    rootfiles = container.findall('.//c:rootfile', ns)
    if not rootfiles:
        raise ValueError('Invalid EPUB: no rootfile found in container.xml')
    if require_unique and len(rootfiles) != 1:
        raise ValueError('EPUB has ambiguous publication packages')
    opf_path = rootfiles[0].get('full-path')
    opf_xml = read_member(opf_path)
    try:
        package = ET.fromstring(opf_xml)
    except ET.ParseError:
        # Preserve the narrow repair used for two verified Sigil-generated
        # books; unrelated malformed XML must still fail.
        repaired = opf_xml.replace(b' refines">"#pub-i"', b'')
        if repaired == opf_xml:
            raise
        package = ET.fromstring(repaired)
    return opf_path, package
