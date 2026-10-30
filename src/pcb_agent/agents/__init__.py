"""三个角色：需求分析、电路设计、评审。

每个角色一个文件、一份 system prompt、一组白名单能力。
换角色不是换名字，而是换「它能做什么、不能做什么」。
"""

from .analyst import AnalystAgent
from .critic import CriticAgent
from .designer import DesignerAgent

__all__ = ["AnalystAgent", "DesignerAgent", "CriticAgent"]
