"""Shared container/OPF parsing for text extraction and publication review."""
import os
from pathlib import PurePosixPath, PureWindowsPath
import re
import sys
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

# ``alexandria_alignment`` lives at the repository root. Make this module
# importable on its own (app process, isolated test module, publication review)
# without requiring ``utils`` to have run first; this is the root insertion
# utils itself performs.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from alexandria_alignment import (get_epub_declared_encoding,
                                  is_unusable_epub_codec)

# EPUB 3 calls manifest `properties` a space-separated list; XML separates the
# tokens with space, tab, CR or LF. str.split() would also split on NBSP and
# other Unicode spaces, which that grammar does not treat as separators. Form
# feed is kept for tolerance even though XML 1.0 forbids it in attribute values.
_XML_WHITESPACE = re.compile(r'[ \t\n\f\r]+')

# ET.fromstring(bytes) refuses an XML encoding declaration it cannot process in
# two ways on CPython 3.10: LookupError for a codec Expat does not know, and
# ValueError('multi-byte encodings are not supported') for one it cannot stream
# (shift_jis, utf-16-le/be, utf-32 declarations; a BOM-carrying utf-16 document
# is fine). A third shape - the declarations Expat hands to Python's codec
# (``punycode``, ``idna``, ``undefined``) - raises UnicodeError instead; it is
# refused before parsing by the one unusable-codec decision
# (:func:`alexandria_alignment.is_unusable_epub_codec`), not caught here. All
# mean the same thing - the declaration cannot be honoured - and every EPUB
# reader here treats such a document as malformed. The ValueError is matched by
# its exact message; ValueError is also the channel for unrelated failures, and
# an unfixed declaration class must not be mistaken for one.
_EXPAT_MULTIBYTE_REFUSAL = 'multi-byte encodings are not supported'


class EpubXmlEncodingError(ValueError):
    """An XML declaration Expat cannot process for this EPUB resource."""


def is_epub_encoding_refusal(error):
    """Return whether ``error`` is Expat refusing an XML encoding declaration.

    The declarations Expat hands to Python (``punycode``, ``idna``,
    ``undefined``) are classified before parsing by the one codec decision
    (:func:`alexandria_alignment.is_unusable_epub_codec`), so a raw
    ``UnicodeError`` is not a refusal channel here: it would mean the shared
    decision missed a declaration, which the tests must catch instead.
    """
    return (isinstance(error, LookupError)
            or (isinstance(error, ValueError)
                and str(error) == _EXPAT_MULTIBYTE_REFUSAL))


def get_epub_encoding_refusal(error):
    """Return the :class:`EpubXmlEncodingError` for ``error``, or ``None``.

    One translation shared by the tree parser and the streaming SVG probe
    (Rule 15), so both map an encoding refusal to the same documented outcome.
    """
    if is_epub_encoding_refusal(error):
        return EpubXmlEncodingError(str(error))
    return None


def get_epub_declaration_refusal(raw):
    """Return the :class:`EpubXmlEncodingError` for an unusable declaration.

    One pre-parse classification shared by the tree parser and the streaming
    SVG probe (Rule 15): both ask :func:`alexandria_alignment.is_unusable_epub_codec`
    about the declaration bytes Expat itself reads. The declaration is read to
    its closing ``?>`` (bounded by
    :data:`alexandria_alignment._EPUB_XML_DECL_MAX_BYTES`), so padding before
    ``encoding=`` cannot hide a codec; a declaration still unterminated at the
    cap is refused as malformed. ``None`` means the declaration is usable or
    absent.
    """
    declared, present, unterminated = get_epub_declared_encoding(raw)
    if unterminated:
        return EpubXmlEncodingError(
            'XML declaration does not end within the read limit')
    if present and declared is not None and is_unusable_epub_codec(declared):
        return EpubXmlEncodingError(
            f'XML declaration names an unusable encoding: {declared}')
    return None


def parse_epub_xml(raw):
    """Parse bounded EPUB XML with the one encoding-refusal decision (Rule 15).

    The declaration's codec name is classified by
    :func:`alexandria_alignment.is_unusable_epub_codec` - the same rule
    :func:`alexandria_alignment.decode_epub_markup` applies to a chapter - so a
    declaration Expat would hand to Python (``punycode``, ``idna``,
    ``undefined``) is refused before it can raise raw codec text. The two
    Expat refusal classes that remain become :class:`EpubXmlEncodingError`, so
    every caller maps it to the same documented refusal or fallback per reader
    (#2370, #2375). ``raw`` is read by the caller, so the member read stays
    size-bounded.
    """
    refusal = get_epub_declaration_refusal(raw)
    if refusal is not None:
        raise refusal
    try:
        # reviewed: callers bound the member read; expat is the only parser
        return ET.fromstring(raw)  # nosec B314
    except (LookupError, ValueError) as error:
        refusal = get_epub_encoding_refusal(error)
        if refusal is not None:
            raise refusal from error
        raise


def split_epub_href(href):
    """Split an EPUB href, or return ``None`` when it cannot name a member.

    ``urlsplit`` raises ``ValueError`` for an unbalanced IPv6 bracket
    (``http://[``). That href cannot identify an archive member, so every EPUB
    reader treats it like any other external reference instead of aborting the
    import (#2377). This is the one place the decision is made (Rule 15): text
    extraction, read-aloud and publication review all call it.
    """
    try:
        return urlsplit(href)
    except (TypeError, ValueError):
        return None


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

    # bounded member read (size-capped, review mode). Read outside the handler:
    # a missing container.xml raises KeyError from getinfo, which is also a
    # LookupError and must not be reported as a broken declaration.
    container_xml = read_member('META-INF/container.xml')
    try:
        container = parse_epub_xml(container_xml)
    except EpubXmlEncodingError as error:
        # An unprocessable encoding declaration is a malformed package (#2375).
        raise ValueError(
            'Invalid EPUB: container.xml declares an unknown encoding') from error
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
        package = parse_epub_xml(opf_xml)
    except EpubXmlEncodingError as error:
        raise ValueError(
            'Invalid EPUB: package document declares an unknown encoding') from error
    except ET.ParseError:
        # Preserve the narrow repair used for two verified Sigil-generated
        # books; unrelated malformed XML must still fail.
        repaired = opf_xml.replace(b' refines">"#pub-i"', b'')
        if repaired == opf_xml:
            raise
        # bounded member read (size-capped, review mode).
        package = parse_epub_xml(repaired)
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
