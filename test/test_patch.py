from agents.mcp.server import MCPServerStdio
from agents.mcp.server import MCPServerSse
from agentd.patch import patch_openai_with_mcp
from openai import OpenAI

fs_server = MCPServerStdio(
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/"],
    },
    cache_tools_list=True
)

remote_server = MCPServerSse(
    params={
        "url": "https://my-mcp.example.com/v1",
        "headers": {"Authorization": "Bearer sk-foo"},
        "timeout": 10,
    },
    cache_tools_list=True
)

client = patch_openai_with_mcp(OpenAI())  # Get the patched client back

response = client.chat.completions.create(
    #model="gpt-4o",
    #model="claude-3-5-sonnet-20240620",
    model="gemini/gemini-2.0-flash",
    messages=[
        {"role": "user", "content": "List the files in /tmp/ using the tool"}
    ],
    mcp_servers=[fs_server],
    mcp_strict=True
)

print(response.choices[0].message.content)

response = client.embeddings.create(
    model="gemini/text-embedding-004",
    input="Hello world",
)

print(response.data[0].embedding)