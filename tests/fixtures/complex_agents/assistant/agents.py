from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .tools import read_docs, run_sql, search_web

llm = ChatOpenAI(model="gpt-4o")
fast = ChatOpenAI(model="gpt-4o-mini")

researcher = create_react_agent(llm, tools=[search_web, read_docs], prompt="You research the question.")
analyst = create_react_agent(llm, tools=[run_sql], prompt="You answer with data from the warehouse.")
writer = create_react_agent(fast, tools=[], prompt="You write the final answer.")

WORKERS = {"research": researcher, "data": analyst}
