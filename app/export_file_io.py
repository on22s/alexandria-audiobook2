"""Native export reads and atomic publication without interrupting pinned readers."""
import os
import stat


def _windows_api():
    import ctypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    api.CreateFileW.restype = ctypes.c_void_p
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    api.ReplaceFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
    api.ReplaceFileW.restype = ctypes.c_int
    return api


def open_export_reader(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    else:
        import ctypes
        import msvcrt
        api = _windows_api()
        handle = api.CreateFileW(os.fspath(path), 0x80000000, 0x1 | 0x2 | 0x4, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
        except BaseException:
            api.CloseHandle(handle)
            raise
    try:
        return os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise


def replace_export_output(source, destination):
    """Publish atomically; the native Windows fallback retains destination metadata."""
    try:
        os.replace(source, destination)
    except PermissionError as error:
        if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32):
            raise
        for path in (source, destination):
            info = os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise error
        import ctypes
        api = _windows_api()
        if not api.ReplaceFileW(os.path.abspath(destination), os.path.abspath(source), None, 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error()) from error
