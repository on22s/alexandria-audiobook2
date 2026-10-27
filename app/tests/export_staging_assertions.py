"""Check delivery contents and the permanent, payload-free staging registry."""
import os
from pathlib import Path
import stat


def delivery_names(case, parent):
    from export_staging_retirement import DIRECTORY, LOCK
    parent = Path(parent)
    names = {path.name for path in parent.iterdir()}
    if DIRECTORY in names:
        registry = parent / DIRECTORY
        info = registry.lstat()
        case.assertTrue(stat.S_ISDIR(info.st_mode))
        case.assertEqual({LOCK}, {path.name for path in registry.iterdir()})
        owner = (registry / LOCK).lstat()
        case.assertTrue(stat.S_ISREG(owner.st_mode))
        case.assertEqual(1, owner.st_nlink)
        if os.name == 'nt':
            case.assertLessEqual(owner.st_size, 1)
        else:
            case.assertEqual(0o700, stat.S_IMODE(info.st_mode))
            case.assertEqual(0o600, stat.S_IMODE(owner.st_mode))
            case.assertEqual(0, owner.st_size)
            case.assertEqual(os.geteuid(), owner.st_uid)
        names.remove(DIRECTORY)
    return names
