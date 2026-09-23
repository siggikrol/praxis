"""Exec OpenTofu with inherited Linux seccomp rules blocking Internet sockets."""
import ctypes
import errno
import os
import sys

class Comparison(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint), ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]


def block_network():
    lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(Comparison)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7fff0000)  # allow by default
    if not ctx:
        raise RuntimeError('Cannot initialize network isolation')
    try:
        # UNIX sockets are required for provider RPC; all other socket families are denied.
        rule = Comparison(0, 1, 1, 0)  # SCMP_CMP_NE, AF_UNIX
        if lib.seccomp_rule_add_array(ctx, 0x50000 | errno.EPERM,
                                     lib.seccomp_syscall_resolve_name(b'socket'), 1, ctypes.byref(rule)) < 0:
            raise RuntimeError('Cannot configure network isolation')
        # Prevent io_uring from bypassing the socket syscall rule.
        for name in (b'io_uring_setup', b'io_uring_enter', b'io_uring_register'):
            syscall = lib.seccomp_syscall_resolve_name(name)
            if syscall >= 0 and lib.seccomp_rule_add_array(ctx, 0x50000 | errno.EPERM, syscall, 0, None) < 0:
                raise RuntimeError('Cannot configure network isolation')
        if lib.seccomp_load(ctx) < 0:
            raise RuntimeError('Cannot enforce network isolation')
    finally:
        lib.seccomp_release(ctx)

if __name__ == '__main__':
    block_network()
    os.execv(sys.argv[1], sys.argv[1:])
