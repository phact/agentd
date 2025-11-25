"""
Test Programmatic Tool Calling (PTC).

Run with: python test/test_ptc.py
"""
import os
import tempfile
from pathlib import Path

from agents.mcp.server import MCPServerStdio
from agentd.ptc import patch_openai_with_ptc, parse_code_fences, CodeFence
from agentd.tool_decorator import tool, FUNCTION_REGISTRY, SCHEMA_REGISTRY
from openai import OpenAI


# =============================================================================
# Test Code Fence Parser
# =============================================================================

def test_parse_code_fences():
    """Test parsing code fences from content."""
    content = '''
Let me list the files:

```bash:execute
ls -la /tmp
```

And create a script:

```my_script.py:create
print("Hello world")
```

Done!
'''
    fences = parse_code_fences(content)
    assert len(fences) == 2

    assert fences[0].fence_type == 'bash'
    assert fences[0].action == 'execute'
    assert 'ls -la /tmp' in fences[0].content

    assert fences[1].fence_type == 'my_script.py'
    assert fences[1].action == 'create'
    assert 'Hello world' in fences[1].content

    print("✓ test_parse_code_fences passed")


def test_parse_empty_content():
    """Test parsing content with no fences."""
    content = "Just some regular text without any code fences."
    fences = parse_code_fences(content)
    assert len(fences) == 0
    print("✓ test_parse_empty_content passed")


def test_parse_multiple_bash():
    """Test parsing multiple bash commands."""
    content = '''
```bash:execute
echo "First"
```

```bash:execute
echo "Second"
```
'''
    fences = parse_code_fences(content)
    assert len(fences) == 2
    assert all(f.fence_type == 'bash' for f in fences)
    print("✓ test_parse_multiple_bash passed")


# =============================================================================
# Test with Local @tool Functions
# =============================================================================

# Clear any existing registrations
FUNCTION_REGISTRY.clear()
SCHEMA_REGISTRY.clear()


@tool
def add_numbers(a: int, b: int) -> int:
    """Add two numbers together.

    a: First number
    b: Second number
    """
    return a + b


@tool
def greet(name: str) -> str:
    """Greet someone by name.

    name: The name to greet
    """
    return f"Hello, {name}!"


def test_local_tools_registered():
    """Test that local tools are registered."""
    assert 'add_numbers' in FUNCTION_REGISTRY
    assert 'greet' in FUNCTION_REGISTRY
    assert 'add_numbers' in SCHEMA_REGISTRY
    assert 'greet' in SCHEMA_REGISTRY
    print("✓ test_local_tools_registered passed")


# =============================================================================
# Integration Test with MCP Server
# =============================================================================

def test_ptc_with_filesystem():
    """Test PTC with filesystem MCP server."""
    # Create a temporary directory for testing
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        test_file = Path(tmpdir) / "test.txt"
        test_file.write_text("Hello from test file!")

        # Setup MCP server
        fs_server = MCPServerStdio(
            params={
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", tmpdir],
            },
            cache_tools_list=True
        )

        # Patch client
        client = patch_openai_with_ptc(OpenAI(), cwd=tmpdir)

        # Make a request that should trigger code fence generation
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": """You have access to a ./skills/ directory with tools. To discover what's available:
- `ls skills/` to list skill directories
- `cat skills/<name>/SKILL.md` to read a skill's documentation

To execute code, use fenced blocks:
- ```bash:execute - Run a bash command
- ```filename.py:create - Create a Python script in the working directory"""
                },
                {
                    "role": "user",
                    "content": f"List the files in {tmpdir} using a bash command."
                }
            ],
            mcp_servers=[fs_server],
        )

        print(f"\nResponse:\n{response.choices[0].message.content}")
        print("✓ test_ptc_with_filesystem passed")


def test_ptc_simple():
    """Simple test without MCP servers - just local tools."""
    with tempfile.TemporaryDirectory() as tmpdir:
        client = patch_openai_with_ptc(OpenAI(), cwd=tmpdir)

        response = client.chat.completions.create(
            model="anthropic/claude-haiku-4-5",
            messages=[
                {
                    "role": "system",
                    "content": """You can execute bash commands using code fences like:
```bash:execute
your command here
```

Always use this format to run commands."""
                },
                {
                    "role": "user",
                    "content": "Browse the file system and show what skills you have. Only read the beginning of each SKILLS.md"
                }
            ],
        )

        print(f"\nResponse:\n{response.choices[0].message.content}")
        print("✓ test_ptc_simple passed")


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    print("Running PTC tests...\n")

    # Unit tests
    #test_parse_code_fences()
    #test_parse_empty_content()
    #test_parse_multiple_bash()
    #test_local_tools_registered()

    print("\n--- Integration Tests ---\n")

    # Integration tests (require API key)
    if os.environ.get('OPENAI_API_KEY'):
        test_ptc_simple()
        test_ptc_with_filesystem()
    else:
        print("Skipping integration tests (OPENAI_API_KEY not set)")

    print("\n✓ All tests passed!")
