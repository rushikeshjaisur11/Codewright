from fastapi import FastAPI
from pydantic import BaseModel

from assistant.graph import build_graph

app = FastAPI()
workflow = build_graph()


class Ask(BaseModel):
    question: str


@app.post("/chat")
async def chat(body: Ask):
    result = await workflow.ainvoke({"question": body.question})
    return {"answer": result["answer"]}
