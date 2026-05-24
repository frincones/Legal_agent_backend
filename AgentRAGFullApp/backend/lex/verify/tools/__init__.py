"""Sprint M14 · Tools del VerificationAgent.

Cada tool es una función pura async con interfaz uniforme `BaseTool.run(parsed)`.
El ToolDispatcher decide cuáles invocar según `parsed.kind` + `parsed.corte`.
"""
from lex.verify.tools.base import BaseTool, ToolResult, ToolStatus

__all__ = ["BaseTool", "ToolResult", "ToolStatus"]
