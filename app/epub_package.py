"""Shared container/OPF parsing for text extraction and publication review."""
from pathlib import PurePosixPath, PureWindowsPath
import re
import xml.etree.ElementTree as ET

# EPUB 3 calls manifest `properties` a space-separated list; XML separates the
# tokens with space, tab, CR or LF. str.split() would also split on NBSP and
# other Unicode spaces, which that grammar does not treat as separators. Form
# feed is kept for tolerance even though XML 1.0 forbids it in attribute values.
_XML_WHITESPACE = re.compile(r'[ \t\n\f\r]+')


def get_epub_package(archive, *, max_xml_bytes=None, require_unique=False, include_details=False):
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
        path = PurePosixPath(name)
        if not path.parts:
            raise ValueError('Unsafe EPUB package member')
        if require_unique:
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
    repaired_metadata = False
    try:
        package = ET.fromstring(opf_xml)
    except ET.ParseError:
        # Preserve the narrow repair used for two verified Sigil-generated
        # books; unrelated malformed XML must still fail.
        repaired = opf_xml.replace(b' refines">"#pub-i"', b'')
        if repaired == opf_xml:
            raise
        package = ET.fromstring(repaired)
        repaired_metadata = True
    if include_details:
        return opf_path, package, {'rootfile_count': len(rootfiles),
                                   'selected_rootfile_index': 0,
                                   'narrow_refines_repair_applied': repaired_metadata}
    return opf_path, package


def is_navigation_document(properties):
    """Return whether a manifest ``properties`` value names the EPUB nav doc.

    EPUB 3.3 section 5.3.5 describes ``properties`` as a space-separated list.
    This app recognises only the spec's lowercase ``nav`` term, matched as a
    whole token: ``navx`` or ``scripted nav-like`` is not the navigation
    document, and ``NAV`` is not ``nav``. Tokens are split on XML whitespace
    (space, tab, CR, LF); form feed is accepted too but cannot occur in an XML
    1.0 attribute value. Non-XML spaces such as NBSP do not separate tokens.
    One rule for extraction and read-aloud (Rule 15).
    """
    return 'nav' in _XML_WHITESPACE.split(properties or '')
