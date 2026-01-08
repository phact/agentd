# agentd/ptc.py
"""
Programmatic Tool Calling (PTC) - Code fence based tool execution.

Instead of using native JSON tool_calls, the LLM writes code in fenced blocks:
- ```bash:execute - Run a bash command
- ```filename.py:create - Create a Python script

The LLM discovers available tools by exploring the ./skills/ directory.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import types
from contextlib import AsyncExitStack
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Protocol, runtime_checkable

from openai.resources.chat.completions import Completions, AsyncCompletions
from openai.resources.responses import Responses, AsyncResponses

import litellm
import litellm.utils as llm_utils

from agentd.tool_decorator import SCHEMA_REGISTRY, FUNCTION_REGISTRY

logger = logging.getLogger(__name__)

MAX_LOOPS = 20

# =============================================================================
# PTC Guidance (auto-injected into system prompt)
# =============================================================================

PTC_GUIDANCE = """You can execute code using fenced blocks:

To run bash:
```bash:execute
<command>
```

To run Python:
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
- `skills/<name>/SKILL.md` - documentation for each skill
- `skills/<name>/scripts/` - example scripts

When writing multiple code fences:
- Back-to-back fences (no text between) execute in parallel
- If you write text between fences, only the first fence executes - wait for its result before continuing
- Never assume results of a command before seeing them

For conditional actions, use logic within a single fence:
- Bash: `grep -q "MARKER" file.txt && rm file.txt`
- Python: `if "MARKER" in open("file.txt").read(): os.remove("file.txt")`
"""


def _inject_ptc_guidance(messages: list) -> list:
    """Inject PTC guidance into system message or prepend one."""
    messages = list(messages)  # copy
    if messages and messages[0].get("role") == "system":
        messages[0] = {
            **messages[0],
            "content": messages[0]["content"] + "\n\n" + PTC_GUIDANCE
        }
    else:
        messages.insert(0, {"role": "system", "content": PTC_GUIDANCE})
    return messages


def _inject_ptc_guidance_responses(input_data: list | str) -> list:
    """Inject PTC guidance for Responses API input format."""
    if isinstance(input_data, str):
        return [
            {"role": "system", "content": PTC_GUIDANCE},
            {"role": "user", "content": input_data}
        ]

    input_list = list(input_data)  # copy
    if input_list and input_list[0].get("role") == "system":
        input_list[0] = {
            **input_list[0],
            "content": input_list[0]["content"] + "\n\n" + PTC_GUIDANCE
        }
    else:
        input_list.insert(0, {"role": "system", "content": PTC_GUIDANCE})
    return input_list


# =============================================================================
# Code Fence Parser
# =============================================================================

@dataclass
class CodeFence:
    """Represents a parsed code fence from LLM response."""
    fence_type: str   # "bash" or filename like "script.py"
    action: str       # "execute" or "create"
    content: str      # The code inside the fence


# Import OpenAI event types for emitting code execution events
from openai.types.responses import ResponseOutputItemDoneEvent, ResponseCodeInterpreterToolCall
from openai.types.responses.response_code_interpreter_tool_call import OutputLogs

from typing import Iterator, Generator, Any

# =============================================================================
# Display Events - Clean events for UI consumption
# =============================================================================

@dataclass
class TextDelta:
    """Text to display from LLM response."""
    type: str = "text_delta"
    text: str = ""


@dataclass
class CodeExecution:
    """Code that was executed with its output."""
    type: str = "code_execution"
    code: str = ""      # First line is fence_type (bash, python, etc)
    output: str = ""
    status: str = "completed"  # "completed" or "failed"


@dataclass
class TurnEnd:
    """Marks the end of a response turn."""
    type: str = "turn_end"


class _StreamBuffer:
    """Internal buffer for processing stream into display events."""

    FENCE_START = re.compile(r'```\w+(?:\.\w+)?:\w+\n')
    FENCE_FULL = re.compile(r'```\w+(?:\.\w+)?:\w+\n.*?```', re.DOTALL)

    def __init__(self):
        self.buffer = ""
        self.pos = 0

    def add(self, text: str) -> str | None:
        """Add text, return safe text to display (or None)."""
        self.buffer += text
        return self._get_safe_text()

    def _get_safe_text(self) -> str | None:
        """Get text that's safe to display (not inside a fence)."""
        unprinted = self.buffer[self.pos:]

        # Check for fence start
        match = self.FENCE_START.search(unprinted)
        if match:
            safe = unprinted[:match.start()]
            if safe:
                self.pos += len(safe)
                return safe
            return None

        # Check for partial fence at end (hold back)
        for i in range(min(20, len(unprinted)), 0, -1):
            if unprinted[-i:].startswith('`'):
                safe = unprinted[:-i]
                if safe:
                    self.pos += len(safe)
                    return safe
                return None

        # All safe
        if unprinted:
            self.pos = len(self.buffer)
            return unprinted
        return None

    def skip_fence(self):
        """Skip past a complete fence in buffer."""
        unprinted = self.buffer[self.pos:]
        match = self.FENCE_FULL.search(unprinted)
        if match:
            self.pos += match.end()

    def flush(self) -> str | None:
        """Flush remaining text."""
        unprinted = self.buffer[self.pos:]
        if unprinted:
            self.pos = len(self.buffer)
            return unprinted
        return None

    def reset(self):
        """Reset for new turn."""
        self.buffer = ""
        self.pos = 0


def display_events(stream) -> Generator[TextDelta | CodeExecution | TurnEnd, None, None]:
    """
    Wrap a PTC stream to yield clean display events.

    Handles buffering so text and code execution don't overlap.

    Usage:
        stream = client.responses.create(model=..., input=..., stream=True)
        for event in display_events(stream):
            if event.type == "text_delta":
                print(event.text, end="", flush=True)
            elif event.type == "code_execution":
                print(f"\\n$ {event.code}")
                print(event.output)
            elif event.type == "turn_end":
                print()
    """
    buf = _StreamBuffer()

    for event in stream:
        event_type = getattr(event, 'type', None)

        if event_type == 'response.output_text.delta':
            delta = getattr(event, 'delta', '')
            safe_text = buf.add(delta)
            if safe_text:
                yield TextDelta(text=safe_text)

        elif event_type == 'response.output_item.done':
            item = getattr(event, 'item', None)
            if item and getattr(item, 'type', None) == 'code_interpreter_call':
                buf.skip_fence()
                output = ""
                if item.outputs:
                    for out in item.outputs:
                        if hasattr(out, 'logs') and out.logs:
                            output += out.logs
                yield CodeExecution(
                    code=item.code,  # includes fence_type\n prefix
                    output=output,
                    status=item.status
                )

        elif event_type == 'response.created':
            buf.reset()

        elif event_type == 'response.completed':
            remaining = buf.flush()
            if remaining:
                yield TextDelta(text=remaining)
            yield TurnEnd()


def _make_execution_event(
    fence_type: str,
    code: str,
    output: str,
    sequence_number: int,
    output_index: int = 0,
    status: str = "completed"
) -> ResponseOutputItemDoneEvent:
    """Create an OpenAI-compatible code execution event."""
    # Prefix code with fence_type on first line so display_events can parse it
    prefixed_code = f"{fence_type}\n{code}"
    tool_call = ResponseCodeInterpreterToolCall(
        id=f"ptc_{sequence_number}",
        code=prefixed_code,
        container_id="ptc",
        status=status,
        type="code_interpreter_call",
        outputs=[OutputLogs(logs=output, type="logs")] if output else []
    )
    return ResponseOutputItemDoneEvent(
        item=tool_call,
        output_index=output_index,
        sequence_number=sequence_number,
        type="response.output_item.done"
    )


def _parse_xml_function_calls(content: str) -> list[CodeFence]:
    """
    Parse XML function_calls format that Claude sometimes uses.

    Supports:
    <function_calls>
    <invoke name="bash:execute">
    <parameter name="command">ls -la</parameter>
    </invoke>
    </function_calls>

    Returns list of CodeFence objects.
    """
    fences = []

    # Pattern for invoke blocks with name="type:action"
    invoke_pattern = r'<invoke\s+name="(\w+(?:\.\w+)?):(\w+)"[^>]*>(.*?)</invoke>'
    invoke_matches = re.findall(invoke_pattern, content, re.DOTALL)

    for fence_type, action, invoke_content in invoke_matches:
        # Extract parameter content - could be named "command" or just raw content
        param_pattern = r'<parameter[^>]*>(.*?)</parameter>'
        param_match = re.search(param_pattern, invoke_content, re.DOTALL)
        if param_match:
            fence_content = param_match.group(1).strip()
        else:
            # Fallback: use raw content between tags
            fence_content = invoke_content.strip()

        if fence_content:
            fences.append(CodeFence(
                fence_type=fence_type,
                action=action.lower(),
                content=fence_content
            ))

    return fences


def parse_code_fences(content: str) -> list[CodeFence]:
    """
    Parse code execution blocks from LLM response.

    Supports two formats:
    1. Code fences: ```bash:execute ... ```
    2. XML function calls: <invoke name="bash:execute">...</invoke>

    Returns list of CodeFence objects in order of appearance.
    """
    fences = []

    # Pattern 1: ```type:action\ncontent```
    fence_pattern = r'```(\w+(?:\.\w+)?):(\w+)\n(.*?)```'
    fence_matches = re.findall(fence_pattern, content, re.DOTALL)

    for fence_type, action, fence_content in fence_matches:
        fences.append(CodeFence(
            fence_type=fence_type,
            action=action.lower(),
            content=fence_content.strip()
        ))

    # Pattern 2: XML function_calls format
    xml_fences = _parse_xml_function_calls(content)
    fences.extend(xml_fences)

    return fences


def extract_complete_fence(buffer: str) -> CodeFence | None:
    """
    Extract the first complete fence from buffer for streaming.
    Returns None if no complete fence found.
    """
    # Try code fence format first
    fence_pattern = r'```(\w+(?:\.\w+)?):(\w+)\n(.*?)```'
    match = re.search(fence_pattern, buffer, re.DOTALL)
    if match:
        return CodeFence(
            fence_type=match.group(1),
            action=match.group(2).lower(),
            content=match.group(3).strip()
        )

    # Try XML format
    xml_fences = _parse_xml_function_calls(buffer)
    if xml_fences:
        return xml_fences[0]

    return None


def remove_fence_from_buffer(buffer: str, fence: CodeFence) -> str:
    """Remove the first occurrence of a fence from the buffer."""
    # Try code fence format
    fence_pattern = r'```' + re.escape(fence.fence_type) + r':' + re.escape(fence.action) + r'\n.*?```'
    new_buffer = re.sub(fence_pattern, '', buffer, count=1, flags=re.DOTALL)
    if new_buffer != buffer:
        return new_buffer

    # Try XML format
    xml_pattern = r'<invoke\s+name="' + re.escape(fence.fence_type) + r':' + re.escape(fence.action) + r'"[^>]*>.*?</invoke>'
    return re.sub(xml_pattern, '', buffer, count=1, flags=re.DOTALL)


def strip_content_after_fences(content: str) -> str:
    """
    Strip potentially hallucinated content after code fences.

    Rules:
    - If text exists between fences: keep only up to end of first fence
    - If no text between fences: keep up to end of last fence
    - Always strip text after the final fence

    Handles both code fence (```type:action...```) and XML invoke formats.
    """
    # Patterns for both formats
    code_fence_pattern = r'```\w+(?:\.\w+)?:\w+\n.*?```'
    xml_pattern = r'<invoke\s+name="\w+(?:\.\w+)?:\w+"[^>]*>.*?</invoke>'

    # Find all fence matches with their positions
    fence_matches = []
    for match in re.finditer(code_fence_pattern, content, re.DOTALL):
        fence_matches.append((match.start(), match.end()))
    for match in re.finditer(xml_pattern, content, re.DOTALL):
        fence_matches.append((match.start(), match.end()))

    if not fence_matches:
        return content  # No fences, return unchanged

    # Sort by start position
    fence_matches.sort(key=lambda x: x[0])

    # Check if there's non-whitespace text between any consecutive fences
    has_text_between = False
    for i in range(len(fence_matches) - 1):
        end_of_current = fence_matches[i][1]
        start_of_next = fence_matches[i + 1][0]
        between_text = content[end_of_current:start_of_next]
        if between_text.strip():
            has_text_between = True
            break

    if has_text_between:
        # Keep only up to end of first fence
        return content[:fence_matches[0][1]]
    else:
        # Keep up to end of last fence
        return content[:fence_matches[-1][1]]


# =============================================================================
# Executor Protocol & Implementations
# =============================================================================

@runtime_checkable
class Executor(Protocol):
    """Protocol for code execution backends."""

    def execute_bash(self, command: str, cwd: Path) -> tuple[str, int]:
        """Run bash command, return (output, exit_code)."""
        ...

    def execute_python(self, code: str, cwd: Path, pythonpath: Path | None = None) -> tuple[str, int]:
        """Run Python code, return (output, exit_code)."""
        ...

    def create_file(self, filename: str, content: str, cwd: Path) -> str:
        """Create file, return confirmation message."""
        ...


class SubprocessExecutor:
    """Default executor using subprocess with persistent shell session."""

    def __init__(self, timeout: int = 60):
        self.timeout = timeout
        self._shell: subprocess.Popen | None = None
        self._shell_cwd: Path | None = None

    def _get_shell(self, cwd: Path) -> subprocess.Popen:
        """Get or create persistent shell for the given cwd."""
        # If shell exists and cwd matches, reuse it
        if self._shell and self._shell.poll() is None and self._shell_cwd == cwd:
            return self._shell

        # Close old shell if exists
        if self._shell:
            self._shell.terminate()
            try:
                self._shell.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._shell.kill()

        # Start new shell with minimal prompt to reduce noise
        env = {
            **os.environ,
            'MCP_BRIDGE_URL': os.environ.get('MCP_BRIDGE_URL', 'http://localhost:8765'),
            'PS1': '',  # Empty prompt
            'PS2': '',
        }
        self._shell = subprocess.Popen(
            ['bash', '--norc', '--noprofile'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            text=True,
            bufsize=1,
            env=env
        )
        self._shell_cwd = cwd
        return self._shell

    def execute_bash(self, command: str, cwd: Path) -> tuple[str, int]:
        """Run bash command in persistent shell session."""
        import uuid
        import time
        import fcntl

        try:
            shell = self._get_shell(cwd)

            # Use unique marker to detect end of output
            marker = f"__END_{uuid.uuid4().hex[:8]}__"

            # Send command with marker and exit code capture
            full_cmd = f'{command}\necho "{marker}$?"\n'
            shell.stdin.write(full_cmd)
            shell.stdin.flush()

            # Set stdout to non-blocking
            fd = shell.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

            # Read output until we see the marker
            output = ""
            exit_code = 0
            deadline = time.time() + self.timeout

            while time.time() < deadline:
                try:
                    chunk = shell.stdout.read(4096)
                    if chunk:
                        output += chunk
                        if marker in output:
                            break
                except BlockingIOError:
                    pass
                time.sleep(0.05)
            else:
                return f"Command timed out after {self.timeout}s", 1

            # Parse output - split on marker
            if marker in output:
                parts = output.split(marker)
                output = parts[0].rstrip('\n')
                try:
                    exit_code = int(parts[1].strip().split('\n')[0])
                except (ValueError, IndexError):
                    exit_code = 0

            return output, exit_code

        except Exception as e:
            return f"Error executing command: {e}", 1

    def execute_python(self, code: str, cwd: Path, pythonpath: Path | None = None) -> tuple[str, int]:
        """Run Python code by writing to temp file and executing."""
        import tempfile
        try:
            # Write code to temp file in cwd
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.py', dir=cwd, delete=False
            ) as f:
                f.write(code)
                temp_path = f.name

            try:
                env = {
                    **os.environ,
                    'MCP_BRIDGE_URL': os.environ.get('MCP_BRIDGE_URL', 'http://localhost:8765')
                }
                # Add pythonpath for imports (e.g. skills/_lib)
                if pythonpath:
                    existing = os.environ.get('PYTHONPATH', '')
                    env['PYTHONPATH'] = f"{pythonpath}:{existing}" if existing else str(pythonpath)

                result = subprocess.run(
                    ['python', temp_path],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    env=env
                )
                output = result.stdout
                if result.stderr:
                    output += f"\n{result.stderr}" if output else result.stderr
                return output.strip(), result.returncode
            finally:
                # Clean up temp file
                Path(temp_path).unlink(missing_ok=True)
        except subprocess.TimeoutExpired:
            return f"Python execution timed out after {self.timeout}s", 1
        except Exception as e:
            return f"Error executing Python: {e}", 1

    def close(self):
        """Close the persistent shell."""
        if self._shell:
            self._shell.terminate()
            try:
                self._shell.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._shell.kill()
            self._shell = None

    def create_file(self, filename: str, content: str, cwd: Path) -> str:
        """Create a file in the working directory."""
        try:
            filepath = cwd / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)
            return f"Created file: {filename}"
        except Exception as e:
            return f"Error creating file {filename}: {e}"

    async def execute_python_async(self, code: str, cwd: Path, pythonpath: Path | None = None) -> tuple[str, int]:
        """Run Python code asynchronously (keeps event loop running for MCP calls)."""
        import tempfile
        try:
            # Write code to temp file in cwd
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.py', dir=cwd, delete=False
            ) as f:
                f.write(code)
                temp_path = f.name

            try:
                env = {
                    **os.environ,
                    'MCP_BRIDGE_URL': os.environ.get('MCP_BRIDGE_URL', 'http://localhost:8765')
                }
                if pythonpath:
                    existing = os.environ.get('PYTHONPATH', '')
                    env['PYTHONPATH'] = f"{pythonpath}:{existing}" if existing else str(pythonpath)

                proc = await asyncio.create_subprocess_exec(
                    'python', temp_path,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env
                )

                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(),
                        timeout=self.timeout
                    )
                    output = stdout.decode()
                    if stderr:
                        err = stderr.decode()
                        output += f"\n{err}" if output else err
                    return output.strip(), proc.returncode or 0
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return f"Python execution timed out after {self.timeout}s", 1
            finally:
                Path(temp_path).unlink(missing_ok=True)
        except Exception as e:
            return f"Error executing Python: {e}", 1

    async def execute_bash_async(self, command: str, cwd: Path) -> tuple[str, int]:
        """Run bash command asynchronously (keeps event loop running for MCP calls)."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.timeout
                )
                output = stdout.decode()
                if stderr:
                    err = stderr.decode()
                    output += f"\n{err}" if output else err
                return output.strip(), proc.returncode or 0
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"Command timed out after {self.timeout}s", 1
        except Exception as e:
            return f"Error executing command: {e}", 1


# =============================================================================
# Skill Generator - Creates skills from MCP tools and @tool functions
# =============================================================================

def _python_type(json_type: str) -> str:
    """Convert JSON schema type to Python type hint."""
    mapping = {
        'string': 'str',
        'integer': 'int',
        'number': 'float',
        'boolean': 'bool',
        'array': 'list',
        'object': 'dict'
    }
    return mapping.get(json_type, 'str')


def generate_tool_function(name: str, schema: dict) -> str:
    """Generate a Python function from tool schema."""
    # Handle both flat and nested schemas
    if 'function' in schema:
        fn = schema['function']
        params = fn.get('parameters', {}).get('properties', {})
        required = fn.get('parameters', {}).get('required', [])
        desc = fn.get('description', '')
    else:
        params = schema.get('parameters', {}).get('properties', {})
        required = schema.get('parameters', {}).get('required', [])
        desc = schema.get('description', '')

    # Build signature
    args = []
    for param, info in params.items():
        type_hint = _python_type(info.get('type', 'string'))
        if param in required:
            args.append(f"{param}: {type_hint}")
        else:
            args.append(f"{param}: {type_hint} = None")

    sig = ", ".join(args)
    call_args = ", ".join(f"{p}={p}" for p in params.keys())

    # Escape docstring
    desc_escaped = desc.replace('"""', '\\"\\"\\"')

    return f'''
def {name}({sig}) -> dict:
    """{desc_escaped}"""
    return _call("{name}", {call_args})
'''


def generate_tools_module(tools: dict[str, dict], bridge_port: int = 8765) -> str:
    """Generate the complete tools.py module with all tool functions."""
    header = f'''"""Auto-generated tool bindings."""
import os
import json
import urllib.request

_BRIDGE_URL = os.environ.get('MCP_BRIDGE_URL', 'http://localhost:{bridge_port}')

def _call(tool_name: str, **kwargs):
    """Call a tool via the bridge."""
    # Filter out None values
    filtered = {{k: v for k, v in kwargs.items() if v is not None}}
    req = urllib.request.Request(
        f"{{_BRIDGE_URL}}/call/{{tool_name}}",
        data=json.dumps(filtered).encode(),
        headers={{'Content-Type': 'application/json'}}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {{"error": f"HTTP {{e.code}}: {{e.reason}}"}}
    except Exception as e:
        return {{"error": str(e)}}

# --- Generated tool functions below ---
'''

    functions = []
    for name, schema in tools.items():
        functions.append(generate_tool_function(name, schema))

    return header + "\n".join(functions)


def _get_example_value(param_name: str, param_schema: dict) -> str:
    """Get an example value for a parameter, using schema hints or smart defaults."""
    # 1. Use 'examples' array if available (JSON Schema standard)
    if 'examples' in param_schema and param_schema['examples']:
        val = param_schema['examples'][0]
        return repr(val)

    # 2. Use 'default' if available
    if 'default' in param_schema:
        return repr(param_schema['default'])

    # 3. Use 'example' (singular, common in OpenAPI)
    if 'example' in param_schema:
        return repr(param_schema['example'])

    # 4. Fall back to smart defaults based on name/type
    param_type = param_schema.get('type', 'string')
    name_lower = param_name.lower()

    if param_type == 'string':
        if 'url' in name_lower:
            return '"https://example.com"'
        elif 'path' in name_lower:
            return '"/path/to/file"'
        elif 'content' in name_lower or 'text' in name_lower or 'body' in name_lower:
            return '"Hello, world!"'
        elif 'name' in name_lower:
            return '"example"'
        elif 'query' in name_lower:
            return '"search term"'
        else:
            return '"value"'
    elif param_type == 'integer' or param_type == 'number':
        if 'length' in name_lower or 'size' in name_lower or 'limit' in name_lower:
            return '1000'
        elif 'port' in name_lower:
            return '8080'
        else:
            return '10'
    elif param_type == 'boolean':
        return 'True'
    elif param_type == 'array':
        return '[]'
    elif param_type == 'object':
        return '{}'
    else:
        return '"value"'


def generate_example_file(tool_name: str, schema: dict) -> str:
    """Generate an example file showing how to call a tool."""
    # Handle both flat schema and nested {type: function, function: {...}} format
    if 'function' in schema:
        schema = schema['function']
    params = schema.get('parameters', {}).get('properties', {})
    required = schema.get('parameters', {}).get('required', [])

    # Build example args
    example_args = []
    for param_name, param_schema in params.items():
        example_val = _get_example_value(param_name, param_schema)
        example_args.append(f'{param_name}={example_val}')

    args_str = ', '.join(example_args) if example_args else ''
    desc = schema.get('description', f'Call the {tool_name} function')

    return f'''"""Example: {tool_name}

{desc}
"""
from lib.tools import {tool_name}

result = {tool_name}({args_str})
print(result)
'''


def generate_skill_md(skill_name: str, description: str, tools: list[str]) -> str:
    """Generate SKILL.md content for a skill directory (AgentSkills spec compliant)."""
    tools_list = "\n".join(f"- `{t}`" for t in tools)
    # Format skill name for frontmatter (lowercase, hyphens)
    formatted_name = skill_name.lower().replace('_', '-')

    return f'''---
name: {formatted_name}
description: {description}
---

# {skill_name}

{description}

## Available Functions

{tools_list}

## Usage

Import from the shared `lib.tools` module:

```python
from lib.tools import {tools[0] if tools else 'function_name'}
result = {tools[0] if tools else 'function_name'}(...)
print(result)
```

See the `scripts/` directory for usage examples.
'''


def _setup_skill_dir(skill_dir: Path, skill_name: str, tools: dict[str, dict], description: str = None):
    """Setup a single skill directory with SKILL.md and scripts/ (AgentSkills spec compliant)."""
    skill_dir.mkdir(exist_ok=True)
    scripts_dir = skill_dir / 'scripts'
    scripts_dir.mkdir(exist_ok=True)

    # Generate example scripts
    for tool_name, schema in tools.items():
        script_file = scripts_dir / f'{tool_name}_example.py'
        script_file.write_text(generate_example_file(tool_name, schema))

    # Generate SKILL.md with frontmatter
    skill_md = generate_skill_md(
        skill_name,
        description or f'Tools from {skill_name}',
        list(tools.keys())
    )
    (skill_dir / 'SKILL.md').write_text(skill_md)


def _setup_shared_lib(skills_dir: Path, all_tools: dict[str, dict], bridge_port: int):
    """Setup the shared lib/ directory at skills root with all tool bindings."""
    lib_dir = skills_dir / 'lib'
    lib_dir.mkdir(exist_ok=True)

    tools_path = lib_dir / 'tools.py'
    init_path = lib_dir / '__init__.py'

    # Generate lib/tools.py with ALL tools
    tools_py = generate_tools_module(all_tools, bridge_port)

    # Only write if tool definitions changed (ignoring port number)
    # The port is read from MCP_BRIDGE_URL env var at runtime anyway
    # This prevents StatReload triggering on every session
    import re
    if tools_path.exists():
        existing = tools_path.read_text()
        # Normalize by removing the port number for comparison
        existing_normalized = re.sub(r'localhost:\d+', 'localhost:PORT', existing)
        new_normalized = re.sub(r'localhost:\d+', 'localhost:PORT', tools_py)
        if existing_normalized == new_normalized:
            # Only port changed - don't rewrite, env var will provide correct port
            return

    tools_path.write_text(tools_py)

    if not init_path.exists():
        init_path.write_text('from .tools import *\n')


async def setup_skills_directory(
    skills_dir: Path,
    mcp_servers: list | None,
    server_cache: dict,
    bridge_port: int | None = None,
    bridge_cache: dict | None = None,
    exit_stack: AsyncExitStack | None = None
) -> tuple[dict[str, any], int]:
    """
    Setup the skills directory with tool bindings and start MCP bridge.

    Directory structure (AgentSkills spec compliant):
        skills/
          lib/                  # Shared - all tool bindings
            __init__.py
            tools.py
          <server_name>/        # One per MCP server
            SKILL.md            # With YAML frontmatter
            scripts/
              tool_example.py
          local/                # @tool decorated functions
            SKILL.md
            scripts/

    Args:
        exit_stack: If provided, MCP servers will be entered as async context
                   managers for proper cleanup. The caller is responsible for
                   closing the exit stack when done.

    Returns:
        Tuple of (server_lookup dict, bridge_port)
    """
    from agentd.mcp_bridge import MCPBridge

    skills_dir.mkdir(parents=True, exist_ok=True)

    server_lookup = {}  # tool_name -> server connection
    all_tools = {}  # All tools for shared lib/tools.py
    skill_configs = []  # (skill_name, tools_dict, description) for each skill

    # Start MCP bridge (reuse if already running)
    # If MCP servers are present, use async mode so tool calls work properly
    if bridge_cache and 'bridge' in bridge_cache:
        bridge = bridge_cache['bridge']
        actual_port = bridge.port
    else:
        bridge = MCPBridge(port=bridge_port or 0)
        if mcp_servers:
            # Use async start - bridge runs in same event loop as MCP connections
            actual_port = await bridge.start_async()
        else:
            # No MCP servers - use thread mode for local tools
            actual_port = bridge.start_in_thread()
        if bridge_cache is not None:
            bridge_cache['bridge'] = bridge

    # 1) Collect tools from each MCP server
    if mcp_servers:
        for server in mcp_servers:
            if server.name not in server_cache:
                # Use context manager if exit_stack provided (proper cleanup)
                if exit_stack is not None:
                    await exit_stack.enter_async_context(server)
                else:
                    await server.connect()
                server_cache[server.name] = server
            conn = server_cache[server.name]

            # Collect tools for this server
            server_tools = {}
            tools = await conn.list_tools()
            for t in tools:
                tool_schema = {
                    'name': t.name,
                    'description': t.description or '',
                    'parameters': t.inputSchema if hasattr(t, 'inputSchema') else {'type': 'object', 'properties': {}}
                }
                server_tools[t.name] = tool_schema
                all_tools[t.name] = tool_schema  # Add to shared tools
                server_lookup[t.name] = conn
                bridge.register_server(t.name, conn)

            # Derive skill name from server
            skill_name = None

            # Check if this looks like an MCP stdio server with command args
            if hasattr(server, 'params'):
                params = server.params
                # params might be a dict or a StdioServerParameters object
                args = getattr(params, 'args', None) or (params.get('args', []) if isinstance(params, dict) else [])
                # Look for package name in args (e.g. "@modelcontextprotocol/server-everything")
                for arg in args:
                    if '/' in str(arg) and not str(arg).startswith('-'):
                        # Extract last component: "server-everything" -> "everything"
                        skill_name = str(arg).split('/')[-1]
                        skill_name = skill_name.replace('server-', '')
                        break

            # Fallback to server name
            if not skill_name:
                skill_name = server.name.split('/')[-1]
                skill_name = skill_name.replace('server-', '')

            # Remove any characters invalid for Python identifiers
            skill_name = ''.join(c if c.isalnum() or c == '_' else '_' for c in skill_name)
            skill_name = skill_name.strip('_')  # Remove leading/trailing underscores
            if not skill_name or skill_name[0].isdigit():
                skill_name = 'mcp_' + skill_name  # Ensure valid identifier

            skill_configs.append((skill_name, server_tools, f'MCP tools from {server.name}'))

    # 2) Collect @tool decorated functions
    if SCHEMA_REGISTRY:
        local_tools = {}
        for name, schema in SCHEMA_REGISTRY.items():
            local_tools[name] = schema
            all_tools[name] = schema  # Add to shared tools
            bridge.register_local_tool(name, FUNCTION_REGISTRY[name])

        if local_tools:
            skill_configs.append(('local', local_tools, 'Locally registered Python tools'))

    # 3) Create shared lib/ with ALL tools
    if all_tools:
        _setup_shared_lib(skills_dir, all_tools, actual_port)

    # 4) Create each skill directory (without lib/)
    for skill_name, tools, description in skill_configs:
        skill_dir = skills_dir / skill_name
        _setup_skill_dir(skill_dir, skill_name, tools, description)

    logger.info(f"Setup {len(skill_configs)} skill(s) at {skills_dir}")
    logger.info(f"Shared lib/ contains {len(all_tools)} tools")
    logger.info(f"MCP Bridge running on http://localhost:{actual_port}")
    return server_lookup, actual_port


# =============================================================================
# PTC Conversation Loop
# =============================================================================

def _run_async(coro):
    """Run an async coroutine from sync context."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _sync_generator_wrapper(async_gen_or_coro):
    """Wrap an async generator for synchronous iteration.

    Uses a persistent event loop in a background thread so that
    aiohttp servers (like MCP bridge) keep running between iterations.

    Handles both:
    - Async generators directly
    - Coroutines that return async generators (need to await first)
    """
    import threading
    import queue
    import inspect

    result_queue = queue.Queue()
    cleanup_queue = queue.Queue()

    def run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def iterate():
            try:
                # Handle coroutine that returns async generator
                gen = async_gen_or_coro
                if inspect.iscoroutine(gen):
                    gen = await gen

                async for item in gen:
                    result_queue.put(('item', item))
                result_queue.put(('done', None))
            except Exception as e:
                result_queue.put(('error', e))

            # Wait for cleanup signal before closing loop
            # This allows graceful shutdown of MCP connections
            cleanup_queue.get(timeout=5)

        loop.run_until_complete(iterate())
        loop.close()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    try:
        while True:
            kind, value = result_queue.get()
            if kind == 'item':
                yield value
            elif kind == 'done':
                break
            elif kind == 'error':
                raise value
    finally:
        # Signal cleanup can proceed
        cleanup_queue.put(True)
        thread.join(timeout=2)


def _extract_content(response, provider: str) -> str:
    """Extract message content from response (provider-agnostic)."""
    if provider == 'openai':
        return response.choices[0].message.content or ""
    else:
        # LiteLLM returns dict
        return response['choices'][0]['message']['content'] or ""


def _format_results(results: list[tuple[CodeFence, str]]) -> str:
    """Format execution results for injection back into conversation."""
    formatted = []
    for fence, output in results:
        if fence.action == 'execute':
            formatted.append(f"```\n$ {fence.content if fence.fence_type == 'bash' else f'python {fence.fence_type}'}\n{output}\n```")
        else:
            formatted.append(f"Created file: {fence.fence_type}")
    return "Execution results:\n" + "\n\n".join(formatted)


async def _handle_ptc_call(
    self,
    args,
    model: str,
    messages: list,
    mcp_servers: list | None,
    cwd: Path,
    executor: Executor,
    kwargs: dict,
    async_mode: bool,
    orig_fn_sync,
    orig_fn_async,
    server_cache: dict,
    bridge_cache: dict | None = None,
    skills_dir_override: Path | None = None
):
    """Handle PTC call with multi-provider support."""
    # Detect provider
    _, provider, api_key, _ = llm_utils.get_llm_provider(model)

    # Setup skills directory and get server lookup
    skills_dir = skills_dir_override or (cwd / 'skills')
    server_lookup, bridge_port = await setup_skills_directory(
        skills_dir, mcp_servers, server_cache, bridge_cache=bridge_cache
    )

    # Set environment variable for subprocess executor
    os.environ['MCP_BRIDGE_URL'] = f'http://localhost:{bridge_port}'

    # Clean kwargs - remove PTC-specific params
    clean_kwargs = {k: v for k, v in kwargs.items()
                    if k not in ('mcp_servers', 'mcp_strict', 'ptc_enabled', 'cwd')}

    # Inject PTC guidance and work with a copy of messages
    current_messages = _inject_ptc_guidance(messages)

    loop_count = 0
    while loop_count < MAX_LOOPS:
        loop_count += 1

        # Call LLM (NO tools parameter - just plain completion)
        if provider == 'openai':
            if async_mode:
                response = await orig_fn_async(
                    self, *args,
                    model=model,
                    messages=current_messages,
                    **clean_kwargs
                )
            else:
                response = orig_fn_sync(
                    self, *args,
                    model=model,
                    messages=current_messages,
                    **clean_kwargs
                )
        else:
            # Use LiteLLM for non-OpenAI providers
            if async_mode:
                response = await litellm.acompletion(
                    model=model,
                    messages=current_messages,
                    api_key=api_key,
                    **clean_kwargs
                )
            else:
                response = litellm.completion(
                    model=model,
                    messages=current_messages,
                    api_key=api_key,
                    **clean_kwargs
                )

        content = _extract_content(response, provider)
        fences = parse_code_fences(content)

        if not fences:
            return response  # No code fences = done

        logger.info(f"Found {len(fences)} code fences to execute")

        # Execute each fence
        # Execute fences
        # Use async execution if MCP servers are present (keeps loop running for callbacks)
        results = []
        for fence in fences:
            if fence.action == 'execute':
                if fence.fence_type == 'bash':
                    # Bash runs in user's cwd
                    if mcp_servers:
                        output, code = await executor.execute_bash_async(fence.content, cwd)
                    else:
                        output, code = executor.execute_bash(fence.content, cwd)
                else:
                    # Python runs in skills_dir so imports work
                    if mcp_servers:
                        output, code = await executor.execute_python_async(fence.content, cwd, pythonpath=skills_dir)
                    else:
                        output, code = executor.execute_python(fence.content, cwd, pythonpath=skills_dir)
                results.append((fence, output))
                logger.info(f"Executed {fence.fence_type}: exit_code={code}")
            elif fence.action == 'create':
                # Files created in skills_dir
                msg = executor.create_file(fence.fence_type, fence.content, skills_dir)
                results.append((fence, msg))
                logger.info(f"Created file: {fence.fence_type}")

        # Append assistant message (stripped of hallucinations) + execution results
        stripped_content = strip_content_after_fences(content)
        current_messages.append({"role": "assistant", "content": stripped_content})
        current_messages.append({"role": "user", "content": _format_results(results)})

    logger.warning(f"Reached max loops ({MAX_LOOPS})")
    return response


async def _handle_ptc_streaming(
    self,
    args,
    model: str,
    messages: list,
    mcp_servers: list | None,
    cwd: Path,
    executor: Executor,
    kwargs: dict,
    async_mode: bool,
    orig_fn_sync,
    orig_fn_async,
    server_cache: dict,
    bridge_cache: dict | None = None,
    skills_dir_override: Path | None = None
):
    """Handle streaming PTC call with sequential fence execution."""
    # Detect provider
    _, provider, api_key, _ = llm_utils.get_llm_provider(model)

    # Clean kwargs
    clean_kwargs = {k: v for k, v in kwargs.items()
                    if k not in ('mcp_servers', 'mcp_strict', 'ptc_enabled', 'cwd', 'stream')}
    clean_kwargs['stream'] = True

    # Inject PTC guidance
    current_messages = _inject_ptc_guidance(messages)
    skills_dir = skills_dir_override or (cwd / 'skills')

    async def stream_with_execution():
        """Generator that manages MCP lifecycle via AsyncExitStack."""
        async with AsyncExitStack() as exit_stack:
            # Setup skills directory with exit stack for proper MCP cleanup
            server_lookup, bridge_port = await setup_skills_directory(
                skills_dir, mcp_servers, server_cache,
                bridge_cache=bridge_cache, exit_stack=exit_stack
            )

            # Set environment variable for subprocess executor
            os.environ['MCP_BRIDGE_URL'] = f'http://localhost:{bridge_port}'

            nonlocal current_messages

            loop_count = 0
            while loop_count < MAX_LOOPS:
                loop_count += 1
                buffer = ""
                executed_fences = []

                # Get stream
                if provider == 'openai':
                    if async_mode:
                        stream = await orig_fn_async(
                            self, *args,
                            model=model,
                            messages=current_messages,
                            **clean_kwargs
                        )
                    else:
                        stream = orig_fn_sync(
                            self, *args,
                            model=model,
                            messages=current_messages,
                            **clean_kwargs
                        )
                else:
                    if async_mode:
                        stream = await litellm.acompletion(
                            model=model,
                            messages=current_messages,
                            api_key=api_key,
                            **clean_kwargs
                        )
                    else:
                        stream = litellm.completion(
                            model=model,
                            messages=current_messages,
                            api_key=api_key,
                            **clean_kwargs
                        )

                # Process stream
                if async_mode:
                    async for chunk in stream:
                        delta = chunk.choices[0].delta.content or ""
                        buffer += delta
                        yield chunk

                        # Check for complete fences
                        while True:
                            fence = extract_complete_fence(buffer)
                            if not fence:
                                break

                            # Execute fence immediately
                            # Use async execution if MCP servers are present
                            if fence.action == 'execute':
                                if fence.fence_type == 'bash':
                                    if mcp_servers:
                                        output, code = await executor.execute_bash_async(fence.content, cwd)
                                    else:
                                        output, code = executor.execute_bash(fence.content, cwd)
                                else:
                                    if mcp_servers:
                                        output, code = await executor.execute_python_async(fence.content, cwd, pythonpath=skills_dir)
                                    else:
                                        output, code = executor.execute_python(fence.content, cwd, pythonpath=skills_dir)
                            else:
                                output = executor.create_file(fence.fence_type, fence.content, skills_dir)

                            executed_fences.append((fence, output))
                            buffer = remove_fence_from_buffer(buffer, fence)
                else:
                    for chunk in stream:
                        delta = chunk.choices[0].delta.content or ""
                        buffer += delta
                        yield chunk

                        while True:
                            fence = extract_complete_fence(buffer)
                            if not fence:
                                break

                            # Use async execution if MCP servers are present
                            if fence.action == 'execute':
                                if fence.fence_type == 'bash':
                                    if mcp_servers:
                                        output, code = await executor.execute_bash_async(fence.content, cwd)
                                    else:
                                        output, code = executor.execute_bash(fence.content, cwd)
                                else:
                                    if mcp_servers:
                                        output, code = await executor.execute_python_async(fence.content, cwd, pythonpath=skills_dir)
                                    else:
                                        output, code = executor.execute_python(fence.content, cwd, pythonpath=skills_dir)
                            else:
                                output = executor.create_file(fence.fence_type, fence.content, skills_dir)

                            executed_fences.append((fence, output))
                            buffer = remove_fence_from_buffer(buffer, fence)

                # If no fences were executed, we're done
                if not executed_fences:
                    return

                # Continue conversation with results (stripped of hallucinations)
                stripped_buffer = strip_content_after_fences(buffer)
                current_messages.append({"role": "assistant", "content": stripped_buffer})
                current_messages.append({"role": "user", "content": _format_results(executed_fences)})
            # Exit stack closes here, properly cleaning up MCP servers

    # Always return async generator - caller handles sync/async bridging
    # via _sync_generator_wrapper for sync calls
    return stream_with_execution()


# =============================================================================
# Responses API Handlers
# =============================================================================

def _extract_responses_content(response, provider: str) -> str:
    """Extract text content from Responses API response."""
    if provider == 'openai':
        # Responses API returns output array
        for item in getattr(response, 'output', []):
            if getattr(item, 'type', None) == 'message':
                for content in getattr(item, 'content', []):
                    if getattr(content, 'type', None) == 'output_text':
                        return getattr(content, 'text', '')
        return ""
    else:
        # LiteLLM format
        output = response.get('output', [])
        for item in output:
            if item.get('type') == 'message':
                for content in item.get('content', []):
                    if content.get('type') == 'output_text':
                        return content.get('text', '')
        return ""


def _format_results_for_responses(results: list[tuple[CodeFence, str]]) -> list[dict]:
    """Format execution results for Responses API input format."""
    formatted = []
    for fence, output in results:
        if fence.action == 'execute':
            text = f"```\n$ {fence.content if fence.fence_type == 'bash' else f'python {fence.fence_type}'}\n{output}\n```"
        else:
            text = f"Created file: {fence.fence_type}"
        formatted.append(text)
    return [{"role": "user", "content": "Execution results:\n" + "\n\n".join(formatted)}]


async def _handle_ptc_responses_call(
    self,
    args,
    model: str,
    input_data: list | str,
    mcp_servers: list | None,
    cwd: Path,
    executor: Executor,
    kwargs: dict,
    async_mode: bool,
    orig_fn_sync,
    orig_fn_async,
    server_cache: dict,
    bridge_cache: dict | None = None,
    skills_dir_override: Path | None = None
):
    """Handle PTC call for Responses API."""
    # Detect provider
    _, provider, api_key, _ = llm_utils.get_llm_provider(model)

    # Setup skills directory
    skills_dir = skills_dir_override or (cwd / 'skills')
    server_lookup, bridge_port = await setup_skills_directory(
        skills_dir, mcp_servers, server_cache, bridge_cache=bridge_cache
    )

    os.environ['MCP_BRIDGE_URL'] = f'http://localhost:{bridge_port}'

    # Clean kwargs
    clean_kwargs = {k: v for k, v in kwargs.items()
                    if k not in ('mcp_servers', 'mcp_strict', 'ptc_enabled', 'cwd')}

    # Inject PTC guidance and normalize input
    current_input = _inject_ptc_guidance_responses(input_data)

    loop_count = 0
    while loop_count < MAX_LOOPS:
        loop_count += 1

        # Call LLM
        if provider == 'openai':
            if async_mode:
                response = await orig_fn_async(
                    self, *args,
                    model=model,
                    input=current_input,
                    **clean_kwargs
                )
            else:
                response = orig_fn_sync(
                    self, *args,
                    model=model,
                    input=current_input,
                    **clean_kwargs
                )
        else:
            if async_mode:
                response = await litellm.aresponses(
                    model=model,
                    input=current_input,
                    api_key=api_key,
                    **clean_kwargs
                )
            else:
                response = litellm.responses(
                    model=model,
                    input=current_input,
                    api_key=api_key,
                    **clean_kwargs
                )

        content = _extract_responses_content(response, provider)
        fences = parse_code_fences(content)

        if not fences:
            return response

        logger.info(f"[Responses] Found {len(fences)} code fences to execute")

        # Execute fences
        # Use async execution if MCP servers are present (keeps loop running for callbacks)
        results = []
        for fence in fences:
            if fence.action == 'execute':
                if fence.fence_type == 'bash':
                    if mcp_servers:
                        output, code = await executor.execute_bash_async(fence.content, cwd)
                    else:
                        output, code = executor.execute_bash(fence.content, cwd)
                else:
                    if mcp_servers:
                        output, code = await executor.execute_python_async(fence.content, cwd, pythonpath=skills_dir)
                    else:
                        output, code = executor.execute_python(fence.content, cwd, pythonpath=skills_dir)
                results.append((fence, output))
            elif fence.action == 'create':
                msg = executor.create_file(fence.fence_type, fence.content, skills_dir)
                results.append((fence, msg))

        # Append to input for next iteration (stripped of hallucinations)
        # Only append assistant message if content is non-empty to avoid Anthropic API errors
        stripped_content = strip_content_after_fences(content)
        if stripped_content.strip():
            current_input.append({"role": "assistant", "content": stripped_content})
        current_input.extend(_format_results_for_responses(results))

    logger.warning(f"[Responses] Reached max loops ({MAX_LOOPS})")
    return response


async def _handle_ptc_responses_streaming(
    self,
    args,
    model: str,
    input_data: list | str,
    mcp_servers: list | None,
    cwd: Path,
    executor: Executor,
    kwargs: dict,
    async_mode: bool,
    orig_fn_sync,
    orig_fn_async,
    server_cache: dict,
    bridge_cache: dict | None = None,
    skills_dir_override: Path | None = None
):
    """Handle streaming PTC call for Responses API."""
    # Detect provider
    _, provider, api_key, _ = llm_utils.get_llm_provider(model)

    skills_dir = skills_dir_override or (cwd / 'skills')

    # Clean kwargs
    clean_kwargs = {k: v for k, v in kwargs.items()
                    if k not in ('mcp_servers', 'mcp_strict', 'ptc_enabled', 'cwd', 'stream')}
    clean_kwargs['stream'] = True

    # Inject PTC guidance and normalize input
    current_input = _inject_ptc_guidance_responses(input_data)

    async def stream_with_execution():
        """Generator that manages MCP lifecycle via AsyncExitStack."""
        async with AsyncExitStack() as exit_stack:
            # Setup skills directory with exit stack for proper MCP cleanup
            server_lookup, bridge_port = await setup_skills_directory(
                skills_dir, mcp_servers, server_cache,
                bridge_cache=bridge_cache, exit_stack=exit_stack
            )

            os.environ['MCP_BRIDGE_URL'] = f'http://localhost:{bridge_port}'

            nonlocal current_input

            loop_count = 0
            seq_num = 0  # Track sequence numbers for events
            while loop_count < MAX_LOOPS:
                loop_count += 1
                buffer = ""
                executed_fences = []

                # Get stream
                if provider == 'openai':
                    if async_mode:
                        stream = await orig_fn_async(
                            self, *args,
                            model=model,
                            input=current_input,
                            **clean_kwargs
                        )
                    else:
                        stream = orig_fn_sync(
                            self, *args,
                            model=model,
                            input=current_input,
                            **clean_kwargs
                        )
                else:
                    if async_mode:
                        stream = await litellm.aresponses(
                            model=model,
                            input=current_input,
                            api_key=api_key,
                            **clean_kwargs
                        )
                    else:
                        stream = litellm.responses(
                            model=model,
                            input=current_input,
                            api_key=api_key,
                            **clean_kwargs
                        )

                # Process stream - extract text from response events
                if async_mode:
                    async for event in stream:
                        yield event
                        seq_num += 1

                        # Extract text from streaming events
                        event_type = getattr(event, 'type', None)
                        if event_type == 'response.output_text.delta':
                            delta = getattr(event, 'delta', '')
                            buffer += delta

                            # Check for complete fences
                            while True:
                                fence = extract_complete_fence(buffer)
                                if not fence:
                                    break

                                # Execute fence
                                # Use async execution if MCP servers are present (keeps loop running for callbacks)
                                if fence.action == 'execute':
                                    if fence.fence_type == 'bash':
                                        if mcp_servers:
                                            output, code = await executor.execute_bash_async(fence.content, cwd)
                                        else:
                                            output, code = executor.execute_bash(fence.content, cwd)
                                    else:
                                        if mcp_servers:
                                            output, code = await executor.execute_python_async(fence.content, cwd, pythonpath=skills_dir)
                                        else:
                                            output, code = executor.execute_python(fence.content, cwd, pythonpath=skills_dir)
                                    status = "completed" if code == 0 else "failed"
                                else:
                                    output = executor.create_file(fence.fence_type, fence.content, skills_dir)
                                    status = "completed"

                                # Emit OpenAI-compatible code interpreter event
                                seq_num += 1
                                yield _make_execution_event(
                                    fence_type=fence.fence_type,
                                    code=fence.content,
                                    output=output,
                                    sequence_number=seq_num,
                                    status=status
                                )

                                executed_fences.append((fence, output))
                                buffer = remove_fence_from_buffer(buffer, fence)
                else:
                    for event in stream:
                        yield event
                        seq_num += 1

                        event_type = getattr(event, 'type', None)
                        if event_type == 'response.output_text.delta':
                            delta = getattr(event, 'delta', '')
                            buffer += delta

                            while True:
                                fence = extract_complete_fence(buffer)
                                if not fence:
                                    break

                                if fence.action == 'execute':
                                    if fence.fence_type == 'bash':
                                        if mcp_servers:
                                            output, code = await executor.execute_bash_async(fence.content, cwd)
                                        else:
                                            output, code = executor.execute_bash(fence.content, cwd)
                                    else:
                                        if mcp_servers:
                                            output, code = await executor.execute_python_async(fence.content, cwd, pythonpath=skills_dir)
                                        else:
                                            output, code = executor.execute_python(fence.content, cwd, pythonpath=skills_dir)
                                    status = "completed" if code == 0 else "failed"
                                else:
                                    output = executor.create_file(fence.fence_type, fence.content, skills_dir)
                                    status = "completed"

                                # Emit OpenAI-compatible code interpreter event
                                seq_num += 1
                                yield _make_execution_event(
                                    fence_type=fence.fence_type,
                                    code=fence.content,
                                    output=output,
                                    sequence_number=seq_num,
                                    status=status
                                )

                                executed_fences.append((fence, output))
                                buffer = remove_fence_from_buffer(buffer, fence)

                # If no fences executed, we're done
                if not executed_fences:
                    return

                # Continue with results (stripped of hallucinations)
                # Only append assistant message if buffer is non-empty to avoid Anthropic API errors
                stripped_buffer = strip_content_after_fences(buffer)
                if stripped_buffer.strip():
                    current_input.append({"role": "assistant", "content": stripped_buffer})
                current_input.extend(_format_results_for_responses(executed_fences))
            # Exit stack closes here, properly cleaning up MCP servers

    if async_mode:
        return stream_with_execution()
    else:
        # For sync streaming, we need a different approach - run entire generator in event loop
        return stream_with_execution()


# =============================================================================
# Client Patching
# =============================================================================

def patch_openai_with_ptc(
    client,
    cwd: str | Path = ".",
    executor: Executor | None = None,
    skills_dir: str | Path | None = None
):
    """
    Patch OpenAI client to use programmatic tool calling.

    Unlike patch_openai_with_mcp, this does NOT pass tools to the LLM.
    Instead, it parses code fences from responses and executes them.

    Supports any model via LiteLLM:
    - OpenAI: "gpt-4o", "gpt-4o-mini"
    - Anthropic: "claude-sonnet-4-20250514"
    - Google: "gemini/gemini-2.0-flash"

    Args:
        client: OpenAI or AsyncOpenAI client
        cwd: Working directory for skill scripts (default: current directory)
        executor: Code execution backend (default: SubprocessExecutor)
        skills_dir: Custom skills directory (default: cwd/skills)

    Returns:
        Patched client
    """
    is_async = client.__class__.__name__ == 'AsyncOpenAI'
    executor = executor or SubprocessExecutor()
    cwd_path = Path(cwd).resolve()
    skills_path = Path(skills_dir).resolve() if skills_dir else None

    # Add per-client caches and config
    client._mcp_server_cache = {}
    client._bridge_cache = {}
    client._skills_dir = skills_path  # Custom skills dir (or None for default)

    # Store original methods
    orig_completions_sync = Completions.create
    orig_completions_async = AsyncCompletions.create

    @wraps(orig_completions_sync)
    def patched_completions_sync(self, *args, model=None, messages=None,
                                  mcp_servers=None, mcp_strict=False,
                                  ptc_enabled=True, stream=False, **kwargs):
        if not ptc_enabled:
            return orig_completions_sync(self, *args, model=model, messages=messages, stream=stream, **kwargs)

        client_obj = getattr(self, '_client', None) or getattr(self, 'client', None)
        server_cache = getattr(client_obj, '_mcp_server_cache', {}) if client_obj else {}
        bridge_cache = getattr(client_obj, '_bridge_cache', {}) if client_obj else {}
        skills_override = getattr(client_obj, '_skills_dir', None) if client_obj else None

        if stream:
            # For sync streaming, wrap async generator directly (don't use _run_async)
            async_gen = _handle_ptc_streaming(
                self, args, model, messages, mcp_servers, cwd_path, executor,
                kwargs, False, orig_completions_sync, orig_completions_async, server_cache, bridge_cache, skills_override
            )
            return _sync_generator_wrapper(async_gen)
        else:
            return _run_async(_handle_ptc_call(
                self, args, model, messages, mcp_servers, cwd_path, executor,
                kwargs, False, orig_completions_sync, orig_completions_async, server_cache, bridge_cache, skills_override
            ))

    @wraps(orig_completions_async)
    async def patched_completions_async(self, *args, model=None, messages=None,
                                         mcp_servers=None, mcp_strict=False,
                                         ptc_enabled=True, stream=False, **kwargs):
        if not ptc_enabled:
            return await orig_completions_async(self, *args, model=model, messages=messages, stream=stream, **kwargs)

        client_obj = getattr(self, '_client', None) or getattr(self, 'client', None)
        server_cache = getattr(client_obj, '_mcp_server_cache', {}) if client_obj else {}
        bridge_cache = getattr(client_obj, '_bridge_cache', {}) if client_obj else {}
        skills_override = getattr(client_obj, '_skills_dir', None) if client_obj else None

        if stream:
            return await _handle_ptc_streaming(
                self, args, model, messages, mcp_servers, cwd_path, executor,
                kwargs, True, orig_completions_sync, orig_completions_async, server_cache, bridge_cache, skills_override
            )
        else:
            return await _handle_ptc_call(
                self, args, model, messages, mcp_servers, cwd_path, executor,
                kwargs, True, orig_completions_sync, orig_completions_async, server_cache, bridge_cache, skills_override
            )

    # Store original Responses methods
    orig_responses_sync = Responses.create
    orig_responses_async = AsyncResponses.create

    @wraps(orig_responses_sync)
    def patched_responses_sync(self, *args, model=None, input=None,
                                mcp_servers=None, mcp_strict=False,
                                ptc_enabled=True, stream=False, **kwargs):
        if not ptc_enabled:
            return orig_responses_sync(self, *args, model=model, input=input, stream=stream, **kwargs)

        client_obj = getattr(self, '_client', None) or getattr(self, 'client', None)
        server_cache = getattr(client_obj, '_mcp_server_cache', {}) if client_obj else {}
        bridge_cache = getattr(client_obj, '_bridge_cache', {}) if client_obj else {}
        skills_override = getattr(client_obj, '_skills_dir', None) if client_obj else None

        if stream:
            # For sync streaming, wrap async generator directly (don't use _run_async)
            async_gen = _handle_ptc_responses_streaming(
                self, args, model, input, mcp_servers, cwd_path, executor,
                kwargs, False, orig_responses_sync, orig_responses_async, server_cache, bridge_cache, skills_override
            )
            return _sync_generator_wrapper(async_gen)
        else:
            return _run_async(_handle_ptc_responses_call(
                self, args, model, input, mcp_servers, cwd_path, executor,
                kwargs, False, orig_responses_sync, orig_responses_async, server_cache, bridge_cache, skills_override
            ))

    @wraps(orig_responses_async)
    async def patched_responses_async(self, *args, model=None, input=None,
                                       mcp_servers=None, mcp_strict=False,
                                       ptc_enabled=True, stream=False, **kwargs):
        if not ptc_enabled:
            return await orig_responses_async(self, *args, model=model, input=input, stream=stream, **kwargs)

        client_obj = getattr(self, '_client', None) or getattr(self, 'client', None)
        server_cache = getattr(client_obj, '_mcp_server_cache', {}) if client_obj else {}
        bridge_cache = getattr(client_obj, '_bridge_cache', {}) if client_obj else {}
        skills_override = getattr(client_obj, '_skills_dir', None) if client_obj else None

        if stream:
            return await _handle_ptc_responses_streaming(
                self, args, model, input, mcp_servers, cwd_path, executor,
                kwargs, True, orig_responses_sync, orig_responses_async, server_cache, bridge_cache, skills_override
            )
        else:
            return await _handle_ptc_responses_call(
                self, args, model, input, mcp_servers, cwd_path, executor,
                kwargs, True, orig_responses_sync, orig_responses_async, server_cache, bridge_cache, skills_override
            )

    # Apply patches
    if is_async:
        client.chat.completions.create = types.MethodType(patched_completions_async, client.chat.completions)
        client.responses.create = types.MethodType(patched_responses_async, client.responses)
    else:
        client.chat.completions.create = types.MethodType(patched_completions_sync, client.chat.completions)
        client.responses.create = types.MethodType(patched_responses_sync, client.responses)

    # Add cleanup method to client
    def cleanup_ptc():
        """Clean up bridge (MCP servers are cleaned up by AsyncExitStack)."""
        # Stop bridge if running
        if 'bridge' in client._bridge_cache:
            bridge = client._bridge_cache['bridge']
            try:
                if hasattr(bridge, '_thread') and bridge._thread:
                    bridge.stop_thread()
            except Exception:
                pass
            client._bridge_cache.clear()

        # Clear server cache (servers already cleaned up by context managers)
        client._mcp_server_cache.clear()

    client.cleanup_ptc = cleanup_ptc

    # Register cleanup on exit
    import atexit
    atexit.register(cleanup_ptc)

    return client
