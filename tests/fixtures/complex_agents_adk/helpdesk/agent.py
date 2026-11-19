import asyncio

from google.adk.agents import BaseAgent, LlmAgent

from .specialists import SPECIALISTS


class FanOutDesk(BaseAgent):
    """Asks every specialist at once, then lets the summarizer answer."""

    async def _run_async_impl(self, ctx):
        streams = [agent.run_async(ctx) for agent in SPECIALISTS]
        results = await asyncio.gather(*(collect(s) for s in streams))
        ctx.session.state["notes"] = results
        async for event in summarizer.run_async(ctx):
            yield event


async def collect(stream):
    return [e async for e in stream]


summarizer = LlmAgent(name="summarizer", model="gemini-2.5-flash", instruction="Summarize {notes}.")
root_agent = FanOutDesk(name="desk", sub_agents=[*SPECIALISTS, summarizer])
