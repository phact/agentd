#!/usr/bin/env python3
"""
PTC Example with @tool decorated functions.

Shows how to register Python functions as tools that the LLM
can discover and call via the skills directory.
"""
import tempfile
from pathlib import Path

from agentd import patch_openai_with_ptc, display_events, tool
from openai import OpenAI


# Register tools with @tool decorator
@tool
def calculate(expression: str) -> str:
    """Safely evaluate a mathematical expression.

    expression: A math expression like "2 + 2" or "sqrt(16)"
    """
    import math
    allowed = {
        'abs': abs, 'round': round, 'min': min, 'max': max,
        'sum': sum, 'pow': pow, 'sqrt': math.sqrt,
        'sin': math.sin, 'cos': math.cos, 'tan': math.tan,
        'pi': math.pi, 'e': math.e,
    }
    try:
        result = eval(expression, {"__builtins__": {}}, allowed)
        return str(result)
    except Exception as e:
        return f"Error: {e}"


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city.

    city: Name of the city
    """
    # Fake weather data for demo
    weather = {
        "new york": "72°F, Partly Cloudy",
        "london": "59°F, Rainy",
        "tokyo": "68°F, Clear",
        "paris": "64°F, Overcast",
    }
    return weather.get(city.lower(), f"Weather data not available for {city}")


@tool
def search_files(pattern: str, directory: str = ".") -> str:
    """Search for files matching a pattern.

    pattern: Glob pattern like "*.py" or "**/*.txt"
    directory: Directory to search in
    """
    from pathlib import Path
    matches = list(Path(directory).glob(pattern))
    if matches:
        return "\n".join(str(m) for m in matches[:20])
    return "No files found"


SYSTEM_PROMPT = """You are an AI assistant with tools available.

To run a bash command:
```bash:execute
<command>
```

To run Python code:
```python:execute
from lib.tools import some_function
result = some_function(arg="value")
print(result)
```

To create a file:
```filename.ext:create
<contents>
```

You have a ./skills/ directory with available tools:
- `skills/lib/tools.py` - shared module with all tool functions
- `skills/<name>/SKILL.md` - documentation for each skill (YAML frontmatter + instructions)
- `skills/<name>/scripts/` - example scripts

To discover skills:
1. `ls skills/` to list available skills
2. `head -5 skills/<name>/SKILL.md` to read frontmatter (name, description)
3. Read full SKILL.md only if you need detailed instructions"""


def main():
    with tempfile.TemporaryDirectory() as workspace:
        print(f"Workspace: {workspace}")
        print("=" * 60)

        client = patch_openai_with_ptc(OpenAI(), cwd=workspace)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "What tools do you have? Then calculate sqrt(144) + 10 and get the weather in Tokyo."
            }
        ]

        print("\n\033[1m🤖 Claude:\033[0m\n")

        stream = client.responses.create(
            model="anthropic/claude-haiku-4-5",
            input=messages,
            stream=True
        )

        for event in display_events(stream):
            if event.type == "text_delta":
                print(event.text, end="", flush=True)
            elif event.type == "code_execution":
                print("\n" + "─" * 50)
                print(f"\033[33m⚡ {event.code}\033[0m")
                if event.output:
                    for line in event.output.split('\n'):
                        print(f"   \033[36m{line}\033[0m")
                if event.status == "failed":
                    print(f"   \033[31m(failed)\033[0m")
                print("─" * 50 + "\n")
            elif event.type == "turn_end":
                print()


if __name__ == "__main__":
    main()
