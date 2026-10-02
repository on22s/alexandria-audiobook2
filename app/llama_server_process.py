"""Stop only the verified executable listening on the configured IPv4 endpoint."""
import os
from pathlib import Path
import select
import signal
import sys

from subprocess_ownership import ensure_pidfd_support, get_process_identity, open_owned_process


def get_loopback_listener_inodes(port):
    inodes = set()
    for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        fields = line.split()
        address, encoded_port = fields[1].split(':')
        if fields[3] == '0A' and int(encoded_port, 16) == port and address in ('0100007F', '00000000'):
            inodes.add(fields[9])
    if not inodes:
        for line in Path('/proc/net/tcp6').read_text().splitlines()[1:]:
            fields = line.split()
            address, encoded_port = fields[1].rsplit(':', 1)
            if (fields[3] == '0A' and int(encoded_port, 16) == port
                    and address == '00000000000000000000000000000000'):
                raise RuntimeError('IPv6 listener on selected port has unverified IPv4 ownership')
    return inodes


def get_listener_processes(inodes):
    owners = set()
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal():
            continue
        try:
            descriptors = list((process/'fd').iterdir())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if target.startswith('socket:[') and target[8:-1] in inodes:
                owners.add(int(process.name))
                break
    return owners


def is_owned_loopback_listener(port, pid):
    if (not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
            or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
        raise ValueError('Expected a valid listener port and process ID')
    inodes = get_loopback_listener_inodes(port)
    return len(inodes) == 1 and get_listener_processes(inodes) == {pid}


def is_expected_listener_launch(port, binary, arguments):
    """Verify the live executable and every launch argument at this endpoint.

    The HTTP health response does not expose KV cache types or GPU offload.
    Inspect the socket owner instead; unreadable or changing ownership fails
    closed rather than accepting an unrelated healthy service.
    """
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Expected a valid listener port')
    inodes = get_loopback_listener_inodes(port)
    owners = get_listener_processes(inodes)
    if len(inodes) != 1 or len(owners) != 1:
        return False
    pid = next(iter(owners))
    birth = get_process_identity(pid)
    expected = os.stat(binary)
    executable = os.stat(f'/proc/{pid}/exe')
    if (expected.st_dev, expected.st_ino) != (executable.st_dev, executable.st_ino):
        return False
    command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    if not command or command[-1] != b'':
        return False
    if command[1:-1] != [os.fsencode(argument) for argument in arguments]:
        return False
    return (get_process_identity(pid) == birth
            and get_loopback_listener_inodes(port) == inodes
            and get_listener_processes(inodes) == {pid})


def stop_llama_server_listener(port, binary, grace=10):
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError('Expected a valid listener port')
    inodes = get_loopback_listener_inodes(port)
    if not inodes:
        return False
    owners = get_listener_processes(inodes)
    if len(inodes) != 1 or len(owners) != 1:
        raise RuntimeError('Configured listener ownership is unreadable or ambiguous')
    pid = next(iter(owners))
    birth = get_process_identity(pid)
    expected = os.stat(binary)
    executable = os.stat(f'/proc/{pid}/exe')
    if (expected.st_dev, expected.st_ino) != (executable.st_dev, executable.st_ino):
        raise RuntimeError('Configured port belongs to another executable; refusing replacement')
    libc = ensure_pidfd_support()
    descriptor = open_owned_process(libc, pid)
    try:
        if (get_process_identity(pid) != birth or get_loopback_listener_inodes(port) != inodes
                or get_listener_processes(inodes) != {pid}):
            raise RuntimeError('Listener identity changed during ownership verification')
        executable = os.stat(f'/proc/{pid}/exe')
        if (expected.st_dev, expected.st_ino) != (executable.st_dev, executable.st_ino):
            raise RuntimeError('Listener executable changed during ownership verification')
        for sig, timeout in ((signal.SIGTERM, grace), (signal.SIGKILL, 5)):
            if libc.pidfd_send_signal(descriptor, sig, None, 0) < 0:
                if select.select([descriptor], [], [], 0)[0]:
                    return True
                raise OSError('Failed to signal selected server through its kernel handle')
            if select.select([descriptor], [], [], timeout)[0]:
                return True
        raise RuntimeError('Selected server did not exit after bounded TERM/KILL cleanup')
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    try:
        if len(sys.argv) >= 4 and sys.argv[1] == '--check-launch':
            if not is_expected_listener_launch(int(sys.argv[2]), sys.argv[3], sys.argv[4:]):
                sys.exit(1)
        elif len(sys.argv) == 4 and sys.argv[1] == '--check-listener-owner':
            if not is_owned_loopback_listener(int(sys.argv[2]), int(sys.argv[3])):
                sys.exit(1)
        elif len(sys.argv) == 3:
            stop_llama_server_listener(int(sys.argv[1]), sys.argv[2])
        else:
            raise ValueError('Expected port and server binary, or --check-listener-owner port pid')
    except (OSError, RuntimeError, ValueError) as error:
        print(f'ensure_llama_server: cannot replace configured server: {error}', file=sys.stderr)
        sys.exit(2)
