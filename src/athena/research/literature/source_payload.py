"""Shared archive identification and size limit for paper source consumers."""

import io
import tarfile

MAX_SOURCE_BYTES = 256 * 1024 * 1024


def is_tar_payload(payload: bytes) -> bool:
    """Recognize a tar source without extracting any files.

    >>> is_tar_payload(b"plain TeX source")
    False
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*"):
            return True
    except tarfile.ReadError:
        # Non-tar input can still be a valid plain or compressed TeX payload.
        return False


if __name__ == "__main__":
    print(is_tar_payload(b"plain TeX source"))
