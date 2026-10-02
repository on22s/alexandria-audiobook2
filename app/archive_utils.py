import os
import shutil
import stat


MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_BYTES = 20 * 1024**3
MIN_EXTRACTION_HEADROOM_BYTES = 64 * 1024**2


def validate_zip_members(zf, dest_dir: str) -> None:
    """Reject ZIP members that escape or exhaust the extraction filesystem."""
    members = zf.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ValueError(f"archive contains more than {MAX_ARCHIVE_MEMBERS} files")
    expanded_bytes = sum(member.file_size for member in members)
    if expanded_bytes > MAX_ARCHIVE_BYTES:
        raise ValueError("archive expands beyond the 20 GB limit")
    # Leave room for allocation overhead, metadata and application writes.
    headroom = max(MIN_EXTRACTION_HEADROOM_BYTES, (expanded_bytes + 19) // 20,
                   len(members) * 4096)
    if expanded_bytes + headroom > shutil.disk_usage(dest_dir).free:
        raise ValueError("archive is larger than the available extraction disk space "
                         f"after reserving {headroom} bytes of headroom")
    destination = os.path.realpath(dest_dir)
    for member in members:
        target = os.path.realpath(os.path.join(dest_dir, member.filename))
        if target != destination and not target.startswith(destination + os.sep):
            raise ValueError(f"archive contains an unsafe path: {member.filename}")
        file_type = stat.S_IFMT(member.external_attr >> 16)
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError(f"archive contains a special file: {member.filename}")


def read_zip_member_bounded(zf, name: str, max_bytes: int) -> bytes:
    """Reject oversize members before decompression and cap the actual read too."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("member byte limit must be a nonnegative integer")
    member = zf.getinfo(name)
    if member.file_size > max_bytes:
        raise ValueError(f"archive member {name!r} exceeds {max_bytes}-byte limit")
    with zf.open(member) as source:
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"archive member {name!r} exceeds {max_bytes}-byte limit")
    return data
