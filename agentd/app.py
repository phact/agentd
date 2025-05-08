import logging

import asyncio

from pydantic import AnyUrl

from agents.mcp.server import MCPServerStdio

import yaml
import traceback
import argparse
from typing import List, Any

from mcp_subscribe.util import call_tool_from_uri
import openai
import dotenv

from agentd.model.config import Config, MCPServerConfig, AgentConfig
from agentd.patch import patch_openai_with_mcp

dotenv.load_dotenv()

# Setup logging configuration early in the file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
# Get logger for this module
logger = logging.getLogger(__name__)


def load_config(path: str) -> Config:
    with open(path, 'r') as f:
        data = yaml.safe_load(f)
    agents = []
    for ag in data.get('agents', []):
        servers = [MCPServerConfig(**server) for server in ag.get('mcp_servers', [])]
        urls = [AnyUrl(url) for url in ag.get('subscriptions', [])]
        agents.append(AgentConfig(
            name=ag['name'],
            model=ag['model'],
            system_prompt=ag['system_prompt'],
            mcp_servers=servers,
            subscriptions=urls
        ))
    return Config(agents=agents)


class Agent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.messages: List[Any] = []
        self.history = [{"role": "system", "content": config.system_prompt}]
        self.sessions_by_tool : dict[str, Any] = {}
        self.servers = []
        self.client = patch_openai_with_mcp(openai.AsyncClient())

    async def handle_notification(self, message: Any):
        self.messages.append(message)

    async def subscribe_resources(self):
        for uri in self.config.subscriptions:
            tool_name = uri.host
            session = self.sessions_by_tool[tool_name]
            await session.subscribe_resource(uri)
            print(f"[{self.config.name}] Subscribed to {uri}")

    async def process_notifications(self):
        while True:
            if self.messages:
                msg = self.messages.pop(0)
                try:
                    uri = msg.root.params.uri
                    print(f"[{self.config.name}] Handling notification: {uri}")
                    tool_name = uri.host
                    session = self.sessions_by_tool[tool_name]
                    try:
                        output = await call_tool_from_uri(uri, session)
                    except Exception as e:
                        print(f"Error calling tool {uri}: {e}")
                        continue
                    self.history.append({"role": "user", "content": f"Tool {uri} returned: {output}"})
                    resp = await self.client.chat.completions.create(
                        model=self.config.model,
                        messages=self.history,
                        mcp_servers=self.servers
                    )
                    content = resp.choices[0].message.content
                    print(f"Assistant: {content}")
                    self.history.append({"role": "assistant", "content": content})
                except Exception:
                    traceback.print_exc()
            await asyncio.sleep(0.5)

    async def process_user_input(self):
        loop = asyncio.get_event_loop()
        while True:
            prompt = await loop.run_in_executor(None, input, f"{self.config.name}> ")
            if prompt.lower() == 'quit':
                break
            self.history.append({"role": "user", "content": prompt})
            try:
                resp = await self.client.chat.completions.create(
                    model=self.config.model,
                    messages=self.history,
                    mcp_servers=self.servers
                )
                content = resp.choices[0].message.content
                print(f"Assistant: {content}")
                self.history.append({"role": "assistant", "content": content})
            except Exception:
                traceback.print_exc()

    async def run(self):
        servers = self.config.mcp_servers

        for server_conf in servers:
            server = MCPServerStdio(
                params={
                    "command": server_conf.command,
                    "args": server_conf.arguments,
                    "env": {kv.split('=',1)[0]: kv.split('=',1)[1] for kv in server_conf.env_vars}
                },
                cache_tools_list=True,
                client_session_timeout_seconds=300
            )

            await server.connect()
            server.session._message_handler = self.handle_notification

            tools = (await server.session.list_tools()).tools
            for tool in tools:
                self.sessions_by_tool[tool.name] = server.session
            self.servers.append(server)

        await self.subscribe_resources()
        print(f"Agent {self.config.name} ready. Type 'quit' to exit.")

        await asyncio.gather(
            self.process_notifications(),
            self.process_user_input()
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Path to YAML config file")
    args = parser.parse_args()
    config = load_config(args.config)

    async def runner():
        await asyncio.gather(*(Agent(ag).run() for ag in config.agents))

    asyncio.run(runner())


if __name__ == '__main__':
    main()
