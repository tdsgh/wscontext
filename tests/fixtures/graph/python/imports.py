from __future__ import annotations

import os
import os.path as osp, json
from pkg.core import Engine, run as go
from . import sibling
from .util import tools
from ..parent.mod import *


def lazy():
    import pkg.lazy
    return pkg.lazy.load()
