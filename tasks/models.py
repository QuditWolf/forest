from __future__ import annotations
from pydantic import BaseModel
from datetime import date
from typing import Optional, List


class Page(BaseModel):
    path: str                         # root-relative path, e.g. "internship/feature-auth/index.md"
    short_id: str = ""                # sha1(path)[:6]
    name: str
    parent_path: Optional[str] = None # canonical path of parent page, None = root-level
    is_folder: bool = False           # True when this page is a directory's index.md
    children: List[str] = []          # canonical paths of immediate child pages

    # Task metadata — all optional; any page can carry these
    state: Optional[str] = None       # todo | in-progress | blocked | waiting | done
    priority: Optional[str] = None    # high | medium | low
    due: Optional[date] = None
    created: date
    completed: Optional[date] = None

    content: str = ""
    backlinks: List[str] = []         # canonical paths of pages that [[link]] to this one


class TreeNode(BaseModel):
    path: str
    name: str
    is_folder: bool = False
    children: List[TreeNode] = []

TreeNode.model_rebuild()
