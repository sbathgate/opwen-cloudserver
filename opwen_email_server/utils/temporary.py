from contextlib import contextmanager
from os import close
from os import remove
from tempfile import mkstemp


def create_tempfilename() -> str:
    file_descriptor, filename = mkstemp()
    close(file_descriptor)
    return filename


@contextmanager
def removing(path: str) -> str:
    try:
        yield path
    finally:
        remove(path)
