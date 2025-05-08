import asyncio
from agents.mcp.server import MCPServerStdio

import yaml
import traceback
import argparse
from typing import List, Any

from mcp_subscribe.util import call_tool_from_uri
import openai
import dotenv

from agentd.model.config import Config, MCPServerConfig, AgentConfig

dotenv.load_dotenv()


def load_config(path: str) -> Config:
    with open(path, 'r') as f:
        data = yaml.safe_load(f)
    agents = []
    for ag in data.get('agents', []):
        servers = [MCPServerConfig(**server) for server in ag.get('mcp_servers', [])]
        agents.append(AgentConfig(
            name=ag['name'],
            model=ag['model'],
            system_prompt=ag['system_prompt'],
            mcp_servers=servers,
            subscriptions=ag.get('subscriptions', [])
        ))
    return Config(agents=agents)


class Agent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.messages: List[Any] = []
        self.history = [{"role": "system", "content": config.system_prompt}]
        #self.session: ClientSession | None = None
        self.session = None
        self.client = openai.AsyncClient()

    async def handle_notification(self, message: Any):
        self.messages.append(message)

    async def subscribe_resources(self):
        for uri in self.config.subscriptions:
            await self.session.subscribe_resource(uri)
            print(f"[{self.config.name}] Subscribed to {uri}")

    async def process_notifications(self):
        while True:
            if self.messages:
                msg = self.messages.pop(0)
                try:
                    uri = msg.root.params.uri
                    print(f"[{self.config.name}] Handling notification: {uri}")
                    output = await call_tool_from_uri(uri, self.session)
                    self.history.append({"role": "user", "content": f"Tool {uri} returned: {output}"})
                    resp = await self.client.chat.completions.create(
                        model=self.config.model,
                        messages=self.history
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
                    messages=self.history
                )
                content = resp.choices[0].message.content
                print(f"Assistant: {content}")
                self.history.append({"role": "assistant", "content": content})
            except Exception:
                traceback.print_exc()

    async def run(self):
        tool = self.config.mcp_servers[0]

        server = MCPServerStdio(
            params={
                "command": tool.command,
                "args": tool.arguments,
                "env": {kv.split('=',1)[0]: kv.split('=',1)[1] for kv in tool.env_vars}
            },
            cache_tools_list=True
        )

        await server.connect()
        self.session = server.session
        server.session._message_handler = self.handle_notification
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
