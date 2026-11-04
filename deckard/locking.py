"""Nonblocking exclusive scan locks on POSIX and Windows."""
import os


def lock_exclusive(stream):
    if os.name == 'nt':
        import msvcrt
        # Windows locks a byte range, including bytes beyond EOF.
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
