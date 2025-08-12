import os
from openai import OpenAI

# Requires OPENAI_API_KEY environment variable
client = OpenAI()

prompt = (
    "Use the built-in code interpreter to calculate factorial(5) "
    "and then print the result."
)

# Request a streaming response using the Responses API
with client.responses.stream(
    model="gpt-4o",
    input=prompt,
    tools=[{
        "type": "code_interpreter",
        "container": {
            "type": "auto"
        }
    }],
) as stream:
    for event in stream:
        if event.type == "response.output_item.added":
            # Every SSE event has a .type field describing what happened
            print(f"{event.output_index:02d} {event.type}: {event.item}")
        elif event.type == "response.output_item.done":
            print(f"{event.output_index:02d} {event.type}: {event.item}")
        elif event.type == "response.done":
            print(f"Response completed: {event.response}")
        print(f"Other: {event}")