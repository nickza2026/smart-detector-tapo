"""Evidence writer admission control. Never deletes existing evidence."""
import shutil
from .frames import image_storage_bytes


class EvidenceStorageError(RuntimeError):
    pass


def admit_evidence(config, size):
    # Atomic publication temporarily needs the full encoded image; reserve also
    # leaves room for SQLite WAL/event metadata and an orderly operational error.
    reserve = 16 * 1024 * 1024
    if image_storage_bytes(config) + size > config.max_image_storage_mb * 1_000_000:
        raise EvidenceStorageError('Evidence quota exhausted')
    if shutil.disk_usage(config.data_dir).free < size + reserve:
        raise EvidenceStorageError('Insufficient evidence disk space')
