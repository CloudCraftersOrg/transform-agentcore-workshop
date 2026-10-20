"""In-memory workspace the agents read from and write to.

Artifacts travel inside the request/response payload, so an agent behaves the same
in-process and on AgentCore Runtime (where nothing is shared on disk).
"""

import posixpath
import tempfile
import threading
from pathlib import Path

from strands import tool


class Workspace:
    def __init__(self, files: dict[str, str] | None = None):
        self.files: dict[str, str] = {}
        self.changed: list[str] = []
        self.lock = threading.RLock()  # tools may run in worker threads; guards read-modify-write edits
        for path, content in (files or {}).items():
            self.files[self.normalize(path)] = content

    @staticmethod
    def normalize(path: str) -> str:
        norm = posixpath.normpath(path.strip())
        if norm.startswith("/") or norm == ".." or norm.startswith("../") or norm in ("", "."):
            raise ValueError(f"invalid workspace path: {path!r}")
        return norm

    def read(self, path: str) -> str:
        return self.files[self.normalize(path)]

    def write(self, path: str, content: str) -> str:
        norm = self.normalize(path)
        with self.lock:
            self.files[norm] = content
            if norm not in self.changed:
                self.changed.append(norm)
        return norm

    def listing(self, prefix: str = "") -> list[str]:
        return sorted(p for p in self.files if p.startswith(prefix))

    def artifacts(self) -> dict[str, str]:
        return {p: self.files[p] for p in self.changed}

    def materialize(self, prefix: str = "") -> Path:
        """Write files under `prefix` to a temp dir (prefix stripped) for CLI validators."""
        root = Path(tempfile.mkdtemp(prefix="ws-"))
        for path, content in self.files.items():
            if prefix and not path.startswith(prefix.rstrip("/") + "/"):
                continue
            rel = path[len(prefix.rstrip("/")) + 1:] if prefix else path
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return root


def workspace_tools(ws: Workspace, writable: bool = True) -> list:
    """Strands tools bound to one workspace instance."""

    @tool
    def list_files(prefix: str = "") -> str:
        """List files in the workspace.

        Args:
            prefix: Only list paths starting with this prefix (e.g. "legacy/app/").
        """
        paths = ws.listing(prefix)
        return "\n".join(f"{p} ({len(ws.files[p])} bytes)" for p in paths) or "(no files)"

    @tool
    def read_file(path: str) -> str:
        """Read a text file from the workspace.

        Args:
            path: Relative path of the file.
        """
        try:
            return ws.read(path)
        except KeyError:
            return f"error: {path} not found. Use list_files to see available paths."

    if not writable:
        return [list_files, read_file]

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a text file in the workspace. Always send the complete file content.

        Args:
            path: Relative path of the file (e.g. "microservice/Dockerfile").
            content: Full file content.
        """
        norm = ws.write(path, content)
        return f"wrote {norm} ({len(content)} bytes)"

    @tool
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace one exact, unique snippet in an existing workspace file.

        Args:
            path: Relative path of the file.
            old: Exact text to replace; must appear exactly once.
            new: Replacement text.
        """
        with ws.lock:
            try:
                current = ws.read(path)
            except KeyError:
                return f"error: {path} not found"
            count = current.count(old)
            if count != 1:
                return f"error: snippet found {count} times in {path}; include more surrounding context so it is unique"
            ws.write(path, current.replace(old, new))
        return f"edited {path}"

    return [list_files, read_file, write_file, edit_file]
