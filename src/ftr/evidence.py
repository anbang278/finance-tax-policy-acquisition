from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from ftr.models import Evidence, digest, utc_now


class EvidenceStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "evidence"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, content: bytes, source_url: str, final_url: str, media_type: str) -> Evidence:
        value = digest(content)
        relative = f"{value[:2]}/{value}"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, target)
        if digest(target.read_bytes()) != value:
            raise RuntimeError("证据哈希校验失败")
        return Evidence(
            evidence_id=value,
            source_url=source_url,
            final_url=final_url,
            sha256=value,
            relative_path=relative,
            media_type=media_type,
            size_bytes=len(content),
            retrieved_at=utc_now(),
        )

    def read(self, evidence: Evidence) -> bytes:
        target = self.root / evidence.relative_path
        content = target.read_bytes()
        if digest(content) != evidence.sha256:
            raise RuntimeError("证据文件已损坏")
        return content
