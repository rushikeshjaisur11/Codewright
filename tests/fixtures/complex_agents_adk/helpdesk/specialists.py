from google.adk.agents import LlmAgent

billing = LlmAgent(name="billing", model="gemini-2.5-flash", instruction="Answer billing questions.")
outages = LlmAgent(name="outages", model="gemini-2.5-flash", instruction="Check for outages.")
accounts = LlmAgent(name="accounts", model="gemini-2.5-pro", instruction="Answer account questions.")

SPECIALISTS = [billing, outages, accounts]
