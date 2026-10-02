"""Darwin task identity and coalition queries; kernel failures never mean empty."""
import ctypes
import errno


class UniqueInfo(ctypes.Structure):
    _fields_ = [('uuid', ctypes.c_uint8 * 16), ('uniqueid', ctypes.c_uint64),
                ('parent_uniqueid', ctypes.c_uint64), ('pidversion', ctypes.c_int32),
                ('parent_pidversion', ctypes.c_int32), ('reserved', ctypes.c_uint64 * 2)]


class CoalitionInfo(ctypes.Structure):
    _fields_ = [('ids', ctypes.c_uint64 * 2), ('reserved', ctypes.c_uint64 * 3)]


class AuditToken(ctypes.Structure):
    _fields_ = [('values', ctypes.c_uint32 * 8)]


class MacApi:
    def __init__(self):
        if (ctypes.sizeof(UniqueInfo), ctypes.sizeof(CoalitionInfo), ctypes.sizeof(AuditToken)) != (56, 40, 32):
            raise RuntimeError('Unsupported native process-info structure layout')
        self.lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        self.lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_listallpids.restype = ctypes.c_int
        self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                         ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_pidinfo.restype = ctypes.c_int
        self.lib.coalition_info_resource_usage.argtypes = [ctypes.c_uint64, ctypes.c_void_p,
                                                          ctypes.c_size_t]
        self.lib.coalition_info_resource_usage.restype = ctypes.c_int
        self.lib.proc_signal_with_audittoken.argtypes = [ctypes.POINTER(AuditToken), ctypes.c_int]
        self.lib.proc_signal_with_audittoken.restype = ctypes.c_int

    def get_info(self, pid):
        if type(pid) is not int or pid <= 0 or pid > 2147483647:
            raise ValueError('Expected a positive native PID')
        before, coalition, after = UniqueInfo(), CoalitionInfo(), UniqueInfo()
        for flavor, data in ((17, before), (20, coalition), (17, after)):
            ctypes.set_errno(0)
            result = self.lib.proc_pidinfo(pid, flavor, 0, ctypes.byref(data), ctypes.sizeof(data))
            if result != ctypes.sizeof(data):
                error = ctypes.get_errno() or errno.EPROTO
                raise OSError(error, f'proc_pidinfo({pid},{flavor}) returned {result}')
        if (before.uniqueid, before.pidversion) != (after.uniqueid, after.pidversion):
            raise OSError(errno.EAGAIN, f'PID {pid} identity changed during coalition observation')
        if not before.uniqueid or not coalition.ids[0]:
            raise OSError(errno.EPROTO, 'Missing native process identity or resource coalition')
        return {'pid': pid, 'pidversion': before.pidversion, 'uniqueid': before.uniqueid,
                'coalition': coalition.ids[0]}

    def get_pids(self):
        ctypes.set_errno(0)
        estimate = self.lib.proc_listallpids(None, 0)
        # libproc can turn a failed proc_listpids call into zero, not minus one.
        if estimate <= 0:
            raise OSError(ctypes.get_errno() or errno.EPROTO, 'Native process enumeration failed')
        capacity = estimate + 64
        for _ in range(8):
            # A diagnostic memory bound must refuse a truncated result, never certify it.
            if capacity > 131072:
                raise OSError(errno.ENOMEM, 'Native process enumeration exceeds probe buffer budget')
            pids = (ctypes.c_int * capacity)()
            ctypes.set_errno(0)
            count = self.lib.proc_listallpids(pids, ctypes.sizeof(pids))
            if count <= 0 or count > capacity:
                raise OSError(ctypes.get_errno() or errno.EPROTO, 'Invalid native process enumeration')
            if count < capacity:
                return sorted({pid for pid in pids[:count] if pid > 0})
            capacity *= 2
        raise OSError(errno.EAGAIN, 'Native process enumeration kept filling its buffer')

    def get_coalition_members(self, coalition):
        if type(coalition) is not int or coalition <= 0 or coalition > 18446744073709551615:
            raise ValueError('Expected a positive native coalition ID')
        members = []
        for pid in self.get_pids():
            try:
                info = self.get_info(pid)
            except ProcessLookupError:
                continue  # A vanished snapshot PID contributes no current process identity.
            # Permission, layout and changing-identity errors must not hide an unknown member.
            if info['coalition'] == coalition:
                members.append(info)
        return members

    def get_active_count(self, coalition):
        if type(coalition) is not int or coalition <= 0 or coalition > 18446744073709551615:
            raise ValueError('Expected a positive native coalition ID')
        # XNU copies MIN(user size, struct size); the first two fields are uint64.
        counters = (ctypes.c_uint64 * 2)()
        ctypes.set_errno(0)
        result = self.lib.coalition_info_resource_usage(coalition, counters, ctypes.sizeof(counters))
        if result:
            raise OSError(ctypes.get_errno() or errno.EPROTO, f'coalition usage returned {result}')
        if counters[1] > counters[0]:
            raise ValueError('coalition exits exceed starts')
        return counters[0] - counters[1]

    def apply_signal(self, info, sig, *, stale=False):
        token = AuditToken()
        token.values[5] = info['pid']
        token.values[7] = (info['pidversion'] + int(stale)) & 0xffffffff
        return self.lib.proc_signal_with_audittoken(ctypes.byref(token), sig)


def is_macos_coalition_reaped(api, coalition):
    """Only ESRCH for a previously observed scope proves terminal removal."""
    try:
        api.get_active_count(coalition)
    except OSError as error:
        if error.errno == errno.ESRCH:
            return True
        raise
    return False
