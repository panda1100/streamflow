import os
import urllib.parse
from pathlib import Path
from typing import Text, IO, Any

from cwltool.stdfsaccess import StdFsAccess, abspath

from streamflow.connector.connector import ConnectorCopyKind
from streamflow.cwl.job_context import SfJobContext
from streamflow.data import remote_fs


class RemoteFsAccess(StdFsAccess):

    def __init__(self, basedir):
        job_context = SfJobContext.current_context()
        if job_context is not None:
            self.remotedir = job_context.get_remote_path(basedir)
            self.target = job_context.target_resource
            self.connector = job_context.connector
        super().__init__(basedir)

    def _remote_abs(self, p):
        relative_path = os.path.relpath(self._abs(p), self.basedir)
        return os.path.join(self.remotedir, relative_path)

    def glob(self, pattern):
        if not self.exists(pattern):
            matches = remote_fs.glob(self.connector, self.target, self._remote_abs(pattern))
            return [Path(os.path.join(self.basedir, os.path.relpath(l, self.remotedir))).as_uri()
                    for l in matches]
        else:
            return super().glob(pattern)

    def open(self, fn, mode):  # type: (Text, str) -> IO[Any]
        if not self.exists(self._abs(fn)):
            self.connector.copy(self._remote_abs(fn), self._abs(fn), self.target, ConnectorCopyKind.remoteToLocal)
        return open(self._abs(fn), mode)

    def size(self, fn):
        if not self.exists(self._abs(fn)):
            return int(self.connector.run(resource=self.target,
                                          command=["stat", "-c \"%s\"", self._remote_abs(fn)],
                                          capture_output=True
                                          ).strip().strip("'\""))
        else:
            return super().size(fn)

    def isfile(self, fn):
        if not self.exists(self._abs(fn)):
            return remote_fs.isfile(self.connector, self.target, self._remote_abs(fn))
        else:
            return super().isfile(fn)

    def isdir(self, fn):
        if not self.exists(self._abs(fn)):
            return remote_fs.isdir(self.connector, self.target, self._remote_abs(fn))
        else:
            return super().isdir(fn)

    def listdir(self, fn):
        if not self.exists(self._abs(fn)):
            dirs = self.connector.run(
                resource=self.target,
                command=["find", self._remote_abs(fn), "-maxdepth", "1", "-mindepth", "1", "-type", "d",
                         "-exec", "basename", "{}", "\\;"],
                capture_output=True
            ).strip()
            return [abspath(urllib.parse.quote(str(l)), fn) for l in dirs]
        else:
            return super().listdir(fn)
