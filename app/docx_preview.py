"""Bounded paragraph/run DOCX extraction with checked styles and inert metadata."""
import hashlib
import io
import json
import re
import stat
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET

POLICY = 'aa2-docx-body-v2'
POLICY_TEXT = ('Paragraphs and headings remain in body order, separated by two LF characters. '
               'Headings have no added markers. Literal Unicode, punctuation, numbers and spaces '
               'are preserved; tabs become TAB and line breaks become LF. Formatting is not '
               'reproduced. Ambiguous or unsupported structures are refused. This is not full '
               'Word layout fidelity. Consecutive intra-paragraph LF breaks can be interpreted '
               'as paragraph boundaries downstream. Batch DOCX, legacy DOC and PDF are unsupported.')
MAX_ARCHIVE = 16 * 1024**2
MAX_ENTRIES = 256
MAX_EXPANDED = 32 * 1024**2
MAX_XML = 8 * 1024**2
MAX_TEXT = 4 * 1024**2
MAX_DEPTH = 64
MAX_NODES = 200000
MAX_RATIO = 100
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
OFFICE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
MAIN_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'
PARTS = {'[Content_Types].xml', '_rels/.rels', 'word/document.xml',
         'word/_rels/document.xml.rels'}
METADATA = {
    'word/' + name + '.xml': (W, name, OFFICE + name,
        'application/vnd.openxmlformats-officedocument.wordprocessingml.' + name + '+xml')
    for name in ('styles', 'settings', 'fontTable', 'webSettings', 'numbering')
}
METADATA['word/fontTable.xml'] = (W, 'fonts', OFFICE + 'fontTable', METADATA['word/fontTable.xml'][3])
METADATA.update({
    'word/theme/theme1.xml': ('http://schemas.openxmlformats.org/drawingml/2006/main', 'theme',
                            OFFICE + 'theme', 'application/vnd.openxmlformats-officedocument.theme+xml'),
    'docProps/core.xml': ('http://schemas.openxmlformats.org/package/2006/metadata/core-properties',
                         'coreProperties', 'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties',
                         'application/vnd.openxmlformats-package.core-properties+xml'),
    'docProps/app.xml': ('http://schemas.openxmlformats.org/officeDocument/2006/extended-properties',
                        'Properties', OFFICE + 'extended-properties',
                        'application/vnd.openxmlformats-officedocument.extended-properties+xml'),
})
PARTS.update(METADATA)
Q = '{' + W + '}'
HARMLESS_ATTRS = {Q + x for x in ('rsidR', 'rsidRPr', 'rsidRDefault', 'rsidP', 'rsidSect', 'rsidDel')}
HARMLESS_ATTRS.update({
    '{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable',
    '{http://schemas.microsoft.com/office/word/2010/wordml}paraId',
    '{http://schemas.microsoft.com/office/word/2010/wordml}textId',
})
PROPERTIES = set(('b bCs i iCs caps smallCaps strike dstrike outline shadow emboss imprint '
    'color spacing w kern position sz szCs highlight u effect bdr shd fitText vertAlign '
    'rtl cs em lang eastAsianLayout snapToGrid rFonts noProof '
    'keepNext keepLines pageBreakBefore widowControl suppressLineNumbers pBdr tabs tab '
    'suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN '
    'bidi adjustRightInd ind contextualSpacing mirrorIndents suppressOverlap jc textDirection '
    'textAlignment textboxTightWrap outlineLvl divId cnfStyle '
    'sectPr type pgSz pgMar paperSrc pgBorders top left bottom right '
    'lnNumType pgNumType cols col formProt vAlign noEndnote titlePg '
    'docGrid printerSettings').split()) - {'printerSettings'}


def validate_properties(node, check_style):
    require_container(node)
    for item in node:
        local = item.tag.removeprefix(Q)
        if local in ('pStyle', 'rStyle'):
            require_structure(item, item.tag, (Q + 'val',))
            check_style(item.get(Q + 'val'), 'paragraph' if local == 'pStyle' else 'character')
        elif local in ('rPr', 'pPr') or local in PROPERTIES:
            if any(not key.startswith(Q) for key in item.attrib):
                raise ValueError('Unsupported DOCX property attributes: ' + item.tag)
        else:
            raise ValueError('Unsupported DOCX property: ' + item.tag)
        validate_properties(item, check_style)


def get_style_checker(parts):
    root = parts.get('word/styles.xml')
    styles, defaults = {}, {}
    if root is not None:
        for style in root.findall(Q + 'style'):
            identity = style.get(Q + 'styleId')
            if not identity or identity in styles:
                raise ValueError('Missing or duplicate DOCX style identity')
            styles[identity] = style
            if style.get(Q + 'default') in ('1', 'true', 'on'):
                kind = style.get(Q + 'type')
                if kind in defaults:
                    raise ValueError('Duplicate DOCX default style')
                defaults[kind] = identity

    def check(identity, kind, chain=()):
        if identity in chain or len(chain) >= MAX_DEPTH:
            raise ValueError('DOCX style inheritance cycle or depth limit')
        if identity not in styles:
            # Legacy minimal fixtures have only literal heading labels.
            if root is None and re.fullmatch(r'Heading[1-9]', identity or ''):
                return
            raise ValueError('Unresolved DOCX style: ' + str(identity))
        style = styles[identity]
        if style.get(Q + 'type') != kind:
            raise ValueError('DOCX style type mismatch')
        for child in style:
            if child.tag == Q + 'basedOn':
                check(child.get(Q + 'val'), kind, chain + (identity,))
            elif child.tag in (Q + 'pPr', Q + 'rPr'):
                validate_properties(child, lambda value, mode: check(value, mode, chain + (identity,)))
            elif child.tag not in {Q + x for x in ('name', 'aliases', 'next', 'link', 'autoRedefine',
                    'hidden', 'uiPriority', 'semiHidden', 'unhideWhenUsed', 'qFormat', 'locked',
                    'personal', 'personalCompose', 'personalReply', 'rsid')}:
                raise ValueError('Unsupported DOCX used style: ' + child.tag)
    if root is not None:
        for default in root.findall(Q + 'docDefaults'):
            for wrapper in default:
                if wrapper.tag not in (Q + 'rPrDefault', Q + 'pPrDefault'):
                    raise ValueError('Unsupported DOCX style defaults')
                validate_properties(wrapper, check)
    for kind, identity in defaults.items():
        if kind in ('paragraph', 'character'):
            check(identity, kind)
    return check


class BoundedTree(ET.TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = self.nodes = 0

    def start(self, tag, attrs):
        self.depth += 1
        self.nodes += 1
        if self.depth > MAX_DEPTH or self.nodes > MAX_NODES:
            raise ValueError('DOCX XML exceeds depth or node limit')
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)

    def doctype(self, name, pubid, system):
        raise ValueError('DOCX XML DTD/entities are unsupported')

    def pi(self, target, text):
        raise ValueError('DOCX XML processing instructions are unsupported')

    def comment(self, text):
        raise ValueError('DOCX XML comments are unsupported')


def parse_xml(raw):
    if len(raw) > MAX_XML:
        raise ValueError('DOCX XML exceeds member limit')
    try:
        value = raw.decode('utf-8-sig')
    except UnicodeError as error:
        raise ValueError('DOCX XML requires UTF-8 encoding') from error
    declaration = re.match(r'<\?xml\s+[^?]*\?>', value)
    if declaration:
        encoding = re.search(r'encoding\s*=\s*[\'"]([^\'"]+)', declaration.group())
        if encoding and encoding[1].lower() not in ('utf-8', 'utf8'):
            raise ValueError('DOCX XML requires UTF-8 encoding')
    return ET.fromstring(value, parser=ET.XMLParser(target=BoundedTree()))


def require_structure(node, tag, attributes=()):
    if node.tag != tag or set(node.attrib) - set(attributes) - (HARMLESS_ATTRS if tag.startswith(Q) else set()):
        raise ValueError('Unsupported DOCX structure or attributes: ' + node.tag)


def require_container(node):
    if (node.text or '').strip() or any((child.tail or '').strip() for child in node):
        raise ValueError('Unsupported DOCX text outside a text run')


def validate_zip_directory(raw):
    """Bound central-directory allocation before ZipFile constructs its index."""
    end = raw.rfind(b'PK\x05\x06', max(0, len(raw) - 65557))
    if end < 0 or end + 22 > len(raw):
        raise ValueError('Malformed DOCX ZIP directory')
    disk, start_disk, disk_count, count, size, offset, comment = struct.unpack_from('<4H2IH', raw, end + 4)
    if (disk or start_disk or disk_count != count or count > MAX_ENTRIES
            or comment > 1024 or end + 22 + comment != len(raw)
            or offset + size != end or not raw.startswith(b'PK\x03\x04')):
        raise ValueError('DOCX ZIP directory exceeds limits or uses unsupported layout')
    position = offset
    for _ in range(count):
        if position + 46 > end or raw[position:position + 4] != b'PK\x01\x02':
            raise ValueError('Malformed DOCX ZIP directory member')
        name, extra, note = struct.unpack_from('<3H', raw, position + 28)
        if name > 256 or extra > 1024 or note > 1024:
            raise ValueError('DOCX ZIP directory member exceeds metadata limits')
        position += 46 + name + extra + note
    if position != end:
        raise ValueError('DOCX ZIP directory entry count mismatch')


def extract_docx_preview(raw):
    """Return exact derived text and its deterministic, original-bound receipt."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ARCHIVE:
        raise ValueError('DOCX exceeds archive limit')
    validate_zip_directory(raw)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise ValueError('DOCX has too many entries')
            seen, parts, total = set(), {}, 0
            for entry in entries:
                name = entry.filename
                mode = entry.external_attr >> 16
                if (name != entry.orig_filename or name.casefold() in seen
                        or name.startswith('/') or '\\' in name or ':' in name
                        or any(piece in ('', '.', '..') for piece in name.split('/'))
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                        or entry.flag_bits & 1
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise ValueError('Unsafe or duplicate DOCX ZIP member: ' + name)
                seen.add(name.casefold())
                if name not in PARTS:
                    raise ValueError('Unsupported DOCX package part: ' + name)
                if (entry.file_size > MAX_XML
                        or entry.file_size > MAX_RATIO * max(1, entry.compress_size)):
                    raise ValueError('DOCX member exceeds size or compression ratio limit')
                total += entry.file_size
                if total > MAX_EXPANDED:
                    raise ValueError('DOCX exceeds expanded size limit')
                with archive.open(entry) as source:
                    content = source.read(min(MAX_XML, MAX_EXPANDED) + 1)
                if len(content) != entry.file_size or len(content) > MAX_XML:
                    raise ValueError('DOCX member exceeds actual size limit')
                parts[name] = parse_xml(content)
    except (zipfile.BadZipFile, ET.ParseError, UnicodeError, RuntimeError, zlib.error, EOFError) as error:
        raise ValueError('Malformed DOCX ZIP/XML: ' + str(error)) from error
    if not {'[Content_Types].xml', '_rels/.rels', 'word/document.xml'} <= parts.keys():
        raise ValueError('DOCX is missing required package parts')
    types = parts['[Content_Types].xml']
    require_structure(types, '{' + CT + '}Types')
    require_container(types)
    declarations = set()
    main = False
    for item in types:
        if item.tag == '{' + CT + '}Default':
            require_structure(item, item.tag, ('Extension', 'ContentType'))
            key = ('default', item.get('Extension'))
            if (item.get('Extension'), item.get('ContentType')) not in (
                    ('rels', 'application/vnd.openxmlformats-package.relationships+xml'),
                    ('xml', 'application/xml')):
                raise ValueError('Unsupported DOCX default content type')
        else:
            require_structure(item, '{' + CT + '}Override', ('PartName', 'ContentType'))
            key = ('override', item.get('PartName'))
            part = item.get('PartName', '').removeprefix('/')
            expected = MAIN_TYPE if part == 'word/document.xml' else METADATA.get(part, (None,) * 4)[3]
            if part not in parts or not item.get('PartName', '').startswith('/') or item.get('ContentType') != expected:
                raise ValueError('Unsupported DOCX content type')
            main = main or part == 'word/document.xml'
        if key in declarations or len(item) or (item.text or '').strip():
            raise ValueError('Duplicate or malformed DOCX content type')
        declarations.add(key)
    if not main:
        raise ValueError('DOCX document content type is missing')
    for name in ('_rels/.rels', 'word/_rels/document.xml.rels'):
        if name not in parts:
            continue
        root = parts[name]
        require_structure(root, '{' + R + '}Relationships')
        require_container(root)
        ids, main_count = set(), 0
        for item in root:
            require_structure(item, '{' + R + '}Relationship', ('Id', 'Type', 'Target'))
            target = item.get('Target', '')
            part = target if name == '_rels/.rels' else 'word/' + target
            expected = OFFICE + 'officeDocument' if name == '_rels/.rels' and part == 'word/document.xml' else METADATA.get(part, (None,) * 4)[2]
            if (not item.get('Id') or item.get('Id') in ids or part not in parts
                    or expected is None or item.get('Type') != expected or len(item)
                    or (item.text or '').strip()):
                raise ValueError('Unsafe or unsupported DOCX package relationship')
            ids.add(item.get('Id'))
            main_count += part == 'word/document.xml'
        if name == '_rels/.rels' and main_count != 1:
            raise ValueError('DOCX requires one main document relationship')
    for name, (namespace, tag, _, _) in METADATA.items():
        if name in parts:
            if parts[name].tag != '{' + namespace + '}' + tag:
                raise ValueError('Invalid DOCX metadata root: ' + name)
            if name == 'word/settings.xml' and parts[name].find(Q + 'trackRevisions') is not None:
                raise ValueError('Unsupported DOCX trackRevisions setting')
    check_style = get_style_checker(parts)
    document = parts['word/document.xml']
    require_structure(document, '{' + W + '}document')
    require_container(document)
    if len(document) != 1:
        raise ValueError('DOCX requires one body')
    body = document[0]
    require_structure(body, '{' + W + '}body')
    require_container(body)
    paragraphs = []
    for paragraph in body:
        if paragraph.tag == Q + 'sectPr':
            validate_properties(paragraph, check_style)
            continue
        require_structure(paragraph, '{' + W + '}p')
        require_container(paragraph)
        fragments = []
        for index, run in enumerate(paragraph):
            if run.tag in (Q + 'bookmarkStart', Q + 'bookmarkEnd', Q + 'proofErr'):
                require_structure(run, run.tag, tuple(Q + x for x in ('id', 'name', 'type', 'colFirst', 'colLast')))
                if len(run) or (run.text or '').strip():
                    raise ValueError('Unsupported DOCX marker content')
                continue
            if run.tag == '{' + W + '}pPr' and index == 0:
                require_structure(run, run.tag)
                require_container(run)
                validate_properties(run, check_style)
                continue
            require_structure(run, '{' + W + '}r')
            require_container(run)
            for item in run:
                if item.tag == Q + 'rPr':
                    validate_properties(item, check_style)
                    continue
                if item.tag == Q + 'lastRenderedPageBreak':
                    require_structure(item, item.tag)
                    if len(item) or item.text:
                        raise ValueError('Unsupported DOCX rendered break content')
                    continue
                if item.tag == '{' + W + '}t':
                    require_structure(item, item.tag, (XML_SPACE,))
                    if item.get(XML_SPACE) not in (None, 'preserve', 'default'):
                        raise ValueError('Unsupported DOCX space policy')
                    fragments.append(item.text or '')
                elif item.tag in ('{' + W + '}tab', '{' + W + '}br', '{' + W + '}cr'):
                    require_structure(item, item.tag, (Q + 'type', Q + 'clear') if item.tag == Q + 'br' else ())
                    if item.get(Q + 'type') not in (None, 'textWrapping') or item.get(Q + 'clear') not in (None, 'none', 'left', 'right', 'all'):
                        raise ValueError('Unsupported DOCX break type')
                    if item.text:
                        raise ValueError('Unsupported DOCX break content')
                    fragments.append('\t' if item.tag.endswith('}tab') else '\n')
                else:
                    raise ValueError('Unsupported DOCX run content: ' + item.tag)
                if len(item):
                    raise ValueError('Unsupported nested DOCX run content')
        paragraphs.append(''.join(fragments))
    text = '\n\n'.join(paragraphs)
    encoded = text.encode('utf-8')
    if not text.strip() or len(encoded) > MAX_TEXT:
        raise ValueError('DOCX text is empty or exceeds extracted text limit')
    receipt = {'policy': POLICY, 'policy_text': POLICY_TEXT,
               'original_sha256': hashlib.sha256(raw).hexdigest(),
               'text_sha256': hashlib.sha256(encoded).hexdigest(),
               'warnings': ['Formatting and page layout are not reproduced; only the supported body profile is accepted.',
                            'Known package metadata is XML-validated then ignored; used styles and defaults are checked for unsupported properties.',
                            'Consecutive intra-paragraph LF breaks can become paragraph boundaries downstream.']}
    digest = hashlib.sha256(json.dumps(receipt, sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':')).encode('utf-8')).hexdigest()
    return {**receipt, 'preview_digest': digest, 'text': text}
