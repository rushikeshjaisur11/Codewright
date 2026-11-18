from langchain_core.tools import tool


@tool
def search_web(query: str) -> str:
    """Search the web."""
    return ""


@tool
def run_sql(sql: str) -> str:
    """Run a read-only SQL query."""
    return ""


@tool
def read_docs(topic: str) -> str:
    """Read internal docs."""
    return ""
