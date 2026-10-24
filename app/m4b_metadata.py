"""Shared native M4B tags, source-clock chapters and FFmetadata serialization."""
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class PublicationCoverSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_book_token: str = Field(strict=True,pattern=r'^[0-9a-f]{64}$')
    expected_revision: str = Field(strict=True,pattern=r'^[0-9a-f]{64}$')
    origin_id: str = Field(strict=True,pattern=r'^[0-9a-f]{64}$')
    reviewed: StrictBool = False


class M4BDeliveryOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    per_chunk_chapters: bool = False
    title: str = Field(default='Audiobook', max_length=20000)
    author: str = Field(default='', max_length=20000)
    narrator: str = Field(default='', max_length=20000)
    year: str = Field(default='', max_length=20000)
    description: str = Field(default='', max_length=20000)
    cover_sha256: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    cover_source: PublicationCoverSelection | None = None

    @model_validator(mode='after')
    def require_source_cover_digest(self):
        if self.cover_source is not None and self.cover_sha256 is None:
            raise ValueError('Source cover selection requires its explicit reviewed digest')
        return self


def escape_m4b_metadata(text):
    for before,after in (('\\','\\\\'),('=','\\='),(';','\\;'),('#','\\#'),('\n',' '),('\r',' ')):
        text = text.replace(before,after)
    return text


def get_m4b_tags(metadata):
    return dict(title=metadata.get('title') if metadata.get('title') is not None else 'Audiobook',
        artist=metadata.get('author') or '', album_artist=metadata.get('narrator') or '',
        date=metadata.get('year') or '', comment=metadata.get('description') or '', genre='Audiobook')


def get_m4b_source_chapters(pcm_plan, groups, source_count):
    ranges = pcm_plan['source_frame_ranges']
    if len(ranges) != source_count:
        raise ValueError('M4B source-frame ranges do not match its chapter sources.')
    # MP4 ends each chapter at the next start, assigning the intervening pause
    # to the preceding chapter. Keep the exact captured source clock.
    starts = [ranges[first][0] for _,first,_ in groups] + [pcm_plan['frames']]
    return [(title, starts[index]/pcm_plan['frame_rate'], starts[index+1]/pcm_plan['frame_rate'])
            for index,(title,_,_) in enumerate(groups)]


def get_m4b_metadata_text(tags, chapters, escape, *, max_bytes=None):
    """Return native escaped metadata; optional cap includes UTF-8 expansion."""
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise ValueError('Invalid M4B metadata allowance')
    lines = [';FFMETADATA1']
    size = len(lines[0].encode('utf-8'))
    def append(line):
        nonlocal size
        size += 1 + len(line.encode('utf-8'))
        if max_bytes is not None and size > max_bytes:
            raise ValueError('M4B metadata exceeds its allowance')
        lines.append(line)
    for key,value in tags.items():
        append(f'{key}={escape(value)}')
    append('')
    for title,start,end in chapters:
        for line in ('[CHAPTER]', 'TIMEBASE=1/1000', f'START={round(start*1000)}',
                     f'END={round(end*1000)}', f'title={escape(title)}', ''):
            append(line)
    return '\n'.join(lines)
