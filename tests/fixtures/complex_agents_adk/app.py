from fastapi import FastAPI
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from helpdesk.agent import root_agent

app = FastAPI()
sessions = InMemorySessionService()
runner = Runner(agent=root_agent, app_name="helpdesk", session_service=sessions)


@app.post("/ask")
async def ask(user: str, text: str):
    answer = ""
    async for event in runner.run_async(user_id=user, session_id=user, new_message=text):
        answer = event
    return {"answer": answer}
