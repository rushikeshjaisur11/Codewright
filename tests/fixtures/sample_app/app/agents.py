from google.adk.agents import LlmAgent, SequentialAgent

planner = LlmAgent(name="planner", model="gemini-2.5-flash")
writer = LlmAgent(name="writer", model="gemini-2.5-flash")

root_agent = SequentialAgent(name="pipeline", sub_agents=[planner, writer])
