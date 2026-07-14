import os
import shutil


MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_BYTES = 20 * 1024**3


def validate_zip_members(zf, dest_dir: str) -> None:
    """Reject ZIP members that escape or exhaust the extraction filesystem."""
    members = zf.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ValueError(f"archive contains more than {MAX_ARCHIVE_MEMBERS} files")
    expanded_bytes = sum(member.file_size for member in members)
    if expanded_bytes > MAX_ARCHIVE_BYTES:
        raise ValueError("archive expands beyond the 20 GB limit")
    if expanded_bytes > shutil.disk_usage(dest_dir).free:
        raise ValueError("archive is larger than the available extraction disk space")
    destination = os.path.realpath(dest_dir)
    for member in members:
        target = os.path.realpath(os.path.join(dest_dir, member.filename))
        if target != destination and not target.startswith(destination + os.sep):
            raise ValueError(f"archive contains an unsafe path: {member.filename}")
