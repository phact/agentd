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


def test_hallucination_after_fence():
    """Test that content after code fences (potential hallucinations) is stripped."""
    from agentd.ptc import strip_content_after_fences

    # Case 1: Single fence with hallucinated output after
    content_with_hallucination = '''Let me check:

```bash:execute
ls -la
```

Here are the files:
- file1.txt
- file2.py
- data/
'''
    stripped = strip_content_after_fences(content_with_hallucination)
    assert "Here are the files" not in stripped, "Hallucinated output should be stripped"
    assert "ls -la" in stripped, "Code fence content should be preserved"
    assert "Let me check" in stripped, "Text before fence should be preserved"

    # Case 2: Multiple fences WITH text between - only first fence kept
    # Text between fences indicates potential dependency, so only execute first
    content_with_text_between = '''First command:

```bash:execute
pwd
```

Second command:

```bash:execute
whoami
```

The current directory is /home/user and the user is root.
'''
    stripped = strip_content_after_fences(content_with_text_between)
    assert "pwd" in stripped, "First fence preserved"
    assert "whoami" not in stripped, "Second fence should be stripped (text between fences)"
    assert "Second command:" not in stripped, "Text between fences stripped"
    assert "current directory is" not in stripped, "Hallucination after stripped"

    # Case 3: Multiple fences WITHOUT text between (back-to-back) - all fences kept
    content_parallel = '''Running both:

```bash:execute
pwd
```
```bash:execute
whoami
```

Output here (hallucinated)
'''
    stripped = strip_content_after_fences(content_parallel)
    assert "pwd" in stripped, "First fence preserved"
    assert "whoami" in stripped, "Second fence preserved (no text between = parallel safe)"
    assert "Output here" not in stripped, "Hallucination after last fence stripped"

    # Case 4: No fence - content unchanged
    no_fence = "Just regular text with no code fences."
    stripped = strip_content_after_fences(no_fence)
    assert stripped == no_fence, "Content without fences should be unchanged"

    # Case 5: Fence at the very end - nothing to strip
    clean_content = '''Check this:

```bash:execute
echo hello
```'''
    stripped = strip_content_after_fences(clean_content)
    assert stripped.strip() == clean_content.strip(), "Clean content should be unchanged"

    print("✓ test_hallucination_after_fence passed")


def test_xml_format_stripping():
    """Test that XML invoke format is also handled by stripping."""
    from agentd.ptc import strip_content_after_fences

    # Single XML invoke with hallucination after
    content_xml = '''Let me check:

<function_calls>
<invoke name="bash:execute">
<parameter name="command">ls -la</parameter>
</invoke>
</function_calls>

Here are the files:
- file1.txt
'''
    stripped = strip_content_after_fences(content_xml)
    assert "ls -la" in stripped, "XML invoke content should be preserved"
    assert "Here are the files" not in stripped, "Hallucination after XML invoke should be stripped"

    # Mixed format with text between
    content_mixed = '''First:

```bash:execute
pwd
```

Now XML:

<function_calls>
<invoke name="bash:execute">
<parameter name="command">whoami</parameter>
</invoke>
</function_calls>
'''
    stripped = strip_content_after_fences(content_mixed)
    assert "pwd" in stripped, "First fence preserved"
    assert "whoami" not in stripped, "Second XML invoke stripped (text between)"

    print("✓ test_xml_format_stripping passed")


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


def test_parse_xml_function_calls():
    """Test parsing XML function_calls format."""
    content = '''
I'll help you explore.
<function_calls>
<invoke name="bash:execute">
<parameter name="command">ls -la skills/</parameter>
</invoke>
</function_calls>

Let me also check Python:
<function_calls>
<invoke name="python:execute">
<parameter name="code">import math
result = math.sqrt(144)
print(result)</parameter>
</invoke>
</function_calls>
'''
    fences = parse_code_fences(content)
    assert len(fences) == 2

    assert fences[0].fence_type == 'bash'
    assert fences[0].action == 'execute'
    assert 'ls -la skills/' in fences[0].content

    assert fences[1].fence_type == 'python'
    assert fences[1].action == 'execute'
    assert 'math.sqrt(144)' in fences[1].content
    print("✓ test_parse_xml_function_calls passed")


def test_parse_mixed_formats():
    """Test parsing mixed code fence and XML formats."""
    content = '''
```bash:execute
echo "fence format"
```

<function_calls>
<invoke name="bash:execute">
<parameter name="command">echo "xml format"</parameter>
</invoke>
</function_calls>
'''
    fences = parse_code_fences(content)
    assert len(fences) == 2
    assert 'fence format' in fences[0].content
    assert 'xml format' in fences[1].content
    print("✓ test_parse_mixed_formats passed")


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

        # Verify skills directory structure (AgentSkills spec compliant)
        skills_dir = Path(tmpdir) / "skills"
        assert skills_dir.exists(), "skills/ directory should be created"

        # Shared lib/ at root
        lib_dir = skills_dir / "lib"
        assert lib_dir.exists(), "skills/lib/ should exist"
        assert (lib_dir / "tools.py").exists(), "skills/lib/tools.py should exist"
        assert (lib_dir / "__init__.py").exists(), "skills/lib/__init__.py should exist"

        # Check tools.py has MCP tool bindings
        tools_content = (lib_dir / "tools.py").read_text()
        assert "def _call(" in tools_content, "tools.py should have _call helper"
        assert "MCP_BRIDGE_URL" in tools_content, "tools.py should reference MCP bridge"

        # MCP server skill directory (filesystem)
        skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir() and d.name != "lib"]
        assert len(skill_dirs) >= 1, "Should have at least one skill directory"

        # Check SKILL.md has frontmatter
        for skill_dir in skill_dirs:
            skill_md = skill_dir / "SKILL.md"
            assert skill_md.exists(), f"{skill_dir.name}/SKILL.md should exist"
            content = skill_md.read_text()
            assert content.startswith("---"), "SKILL.md should have YAML frontmatter"
            assert "name:" in content, "SKILL.md frontmatter should have name"
            assert "description:" in content, "SKILL.md frontmatter should have description"

            # scripts/ directory instead of examples/
            scripts_dir = skill_dir / "scripts"
            assert scripts_dir.exists(), f"{skill_dir.name}/scripts/ should exist"
            assert not (skill_dir / "examples").exists(), "examples/ should not exist (use scripts/)"
            assert not (skill_dir / "lib").exists(), "per-skill lib/ should not exist (use shared)"

        # Verify response exists
        assert response.choices[0].message.content, "Response should have content"

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
                    "content": "Browse the file system and show what skills you have. Only read the beginning of each SKILL.md"
                }
            ],
        )

        # Verify skills directory structure for local tools
        skills_dir = Path(tmpdir) / "skills"
        assert skills_dir.exists(), "skills/ directory should be created"

        # Shared lib/ at root with local tool bindings
        lib_dir = skills_dir / "lib"
        assert lib_dir.exists(), "skills/lib/ should exist"
        tools_py = lib_dir / "tools.py"
        assert tools_py.exists(), "skills/lib/tools.py should exist"

        # Check that local @tool functions are in tools.py
        tools_content = tools_py.read_text()
        assert "add_numbers" in tools_content, "tools.py should have add_numbers binding"
        assert "greet" in tools_content, "tools.py should have greet binding"

        # Local skill directory
        local_skill = skills_dir / "local"
        assert local_skill.exists(), "skills/local/ should exist for @tool functions"

        # SKILL.md with frontmatter
        skill_md = local_skill / "SKILL.md"
        assert skill_md.exists(), "local/SKILL.md should exist"
        md_content = skill_md.read_text()
        assert md_content.startswith("---"), "SKILL.md should have YAML frontmatter"
        assert "name: local" in md_content, "frontmatter should have name: local"

        # scripts/ directory
        scripts_dir = local_skill / "scripts"
        assert scripts_dir.exists(), "local/scripts/ should exist"
        assert not (local_skill / "examples").exists(), "examples/ should not exist"
        assert not (local_skill / "lib").exists(), "per-skill lib/ should not exist"

        # Verify response has content and shows evidence of execution
        content = response.choices[0].message.content
        assert content, "Response should have content"
        # LLM should have explored and found skills
        assert "local" in content.lower() or "skill" in content.lower(), \
            "Response should mention skills found"

        print(f"\nResponse:\n{content}")
        print("✓ test_ptc_simple passed")


# =============================================================================
# Run Tests
# =============================================================================

if __name__ == "__main__":
    print("Running PTC tests...\n")

    # Unit tests
    print("--- Unit Tests ---\n")
    test_parse_code_fences()
    test_parse_empty_content()
    test_parse_multiple_bash()
    test_parse_xml_function_calls()
    test_parse_mixed_formats()
    test_hallucination_after_fence()
    test_xml_format_stripping()
    test_local_tools_registered()

    print("\n--- Integration Tests ---\n")

    # Integration tests (require API key)
    if os.environ.get('OPENAI_API_KEY'):
        test_ptc_simple()
        test_ptc_with_filesystem()
    else:
        print("Skipping integration tests (OPENAI_API_KEY not set)")

    print("\n✓ All tests passed!")
