# openai_mcp_patch.py

import asyncio
import json
import logging
from functools import wraps
from openai.resources.chat.completions import Completions, AsyncCompletions
from openai.resources.embeddings import Embeddings, AsyncEmbeddings
from agents.mcp.util import MCPUtil
import litellm.utils as llm_utils
import litellm

# Global server cache
_SERVER_CACHE = {}

logger = logging.getLogger(__name__)

async def _ensure_connected(server):
    """Return the same connected server every time (stdio or sse)."""
    if server.name not in _SERVER_CACHE:
        await server.connect()
        _SERVER_CACHE[server.name] = server
    return _SERVER_CACHE[server.name]

def _run_async(coro):
    """Run an async coroutine in a new event loop."""
    return asyncio.new_event_loop().run_until_complete(coro)

def patch_openai_with_mcp(client):
    """
    Monkey-patch both sync and async chat.completions.create to:
      - accept mcp_servers=[…] & mcp_strict flag
      - fetch tools via MCPUtil.get_all_function_tools
      - resolve LLM provider via LiteLLM
      - inject headers/base_url accordingly

    Args:
        client: OpenAI or AsyncOpenAI client instance

    Returns:
        The patched client instance
    """
    is_async = client.__class__.__name__ == 'AsyncOpenAI'

    # Store original methods before patching
    orig_completions_sync = Completions.create
    orig_completions_async = AsyncCompletions.create

    orig_embeddings_sync = Embeddings.create
    orig_embeddings_async = AsyncEmbeddings.create


    async def _prepare(servers, strict):
        """Ensure servers are connected and get their tools."""
        connected = [await _ensure_connected(s) for s in servers]
        tools = await MCPUtil.get_all_function_tools(connected, strict)
        return [{
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.params_json_schema
            }
        } for tool in tools]

    def _clean_kwargs(kwargs):
        """Remove our custom kwargs."""
        kwargs = kwargs.copy()
        kwargs.pop('mcp_servers', None)
        kwargs.pop('mcp_strict', None)
        return kwargs


    # constants
    MAX_TOOL_LOOPS = 20          # safety valve

    async def _handle_completion(
            self, args, model, messages,
            mcp_servers, mcp_strict, tools, kwargs, async_mode
    ):
        if mcp_servers and tools:
            raise ValueError("Cannot specify both mcp_servers and tools")

        if mcp_servers:
            tools = await _prepare(mcp_servers, mcp_strict)

        server_lookup = {t.name: s
                         for s in mcp_servers
                         for t in await s.list_tools()}

        _, provider, api_key, _ = llm_utils.get_llm_provider(model)
        clean_kwargs = _clean_kwargs(kwargs)

        if tools and "tool_choice" not in clean_kwargs:
            clean_kwargs["tool_choice"] = "auto"

        # ----- loop until no tool_calls -----
        loop_count = 0
        while True:
            loop_count += 1

            # ----- make the chat completion call -----
            if provider == "openai":
                resp = (
                    await orig_completions_async(self, *args, model=model, messages=messages,
                                     tools=tools, **clean_kwargs)
                    if async_mode else
                    orig_completions_sync(self, *args, model=model, messages=messages,
                              tools=tools, **clean_kwargs)
                )
            else:
                resp = (
                    await litellm.acompletion(model=model, messages=messages,
                                              tools=tools, api_key=api_key,
                                              **clean_kwargs)
                    if async_mode else
                    litellm.completion(model=model, messages=messages,
                                       tools=tools, api_key=api_key,
                                       **clean_kwargs)
                )

            tcalls = getattr(
                resp["choices"][0]["message"], "tool_calls", []
            ) if provider != "openai" else resp.choices[0].message.tool_calls

            if not tcalls or loop_count >= MAX_TOOL_LOOPS:
                if loop_count >= MAX_TOOL_LOOPS:
                    logger.warning(f"Reached max tool loops ({MAX_TOOL_LOOPS})")
                # no more tool calls → done
                return resp

            # consume every tool_call returned in *this* response
            for call in tcalls:
                tname = call["function"]["name"] if provider != "openai" else call.function.name
                targ_json = (call["function"]["arguments"]
                             if provider != "openai"
                             else call.function.arguments)
                if not isinstance(targ_json, dict):
                    targ_json = json.loads(targ_json)

                server = server_lookup.get(tname)
                if not server:
                    raise KeyError(f"Tool '{tname}' not found in server lookup")

                # run the tool via MCP
                logger.info(f"Calling tool {tname} with args {targ_json}")
                result_obj = await server.call_tool(tname, targ_json)

                # append the assistant call + tool response
                messages = messages + [
                    {"role": "assistant", "tool_calls": [call]},
                    {"role": "tool",
                     "name": tname,
                     "content": result_obj.dict().get("content"),
                     "tool_call_id": call["id"] if isinstance(call, dict) else call.id},
                ]

            #clean_kwargs.pop("tools", None)
            #clean_kwargs.pop("tool_choice", None)
            #tools = None   # subsequent turns shouldn't resend schemas

    @wraps(Completions.create)
    def patched_completions_sync(self, *args, model=None, messages=None,
                     mcp_servers=None, mcp_strict=False, tools=None, **kwargs):
        return _run_async(_handle_completion(self, args, model, messages,
                                          mcp_servers, mcp_strict, tools, kwargs, False))

    @wraps(AsyncCompletions.create)
    async def patched_completions_async(self, *args, model=None, messages=None,
                           mcp_servers=None, mcp_strict=False, tools=None, **kwargs):
        return await _handle_completion(self, args, model, messages,
                                     mcp_servers, mcp_strict, tools, kwargs, True)


    @wraps(orig_embeddings_sync)
    def patched_embeddings_sync(self, *args, model=None, input=None, **kwargs):
        # determine provider and api key
        _, provider, api_key, _ = llm_utils.get_llm_provider(model)
        if provider == "openai":
            # call OpenAI SDK
            return orig_embeddings_sync(self, *args, model=model, input=input, **kwargs)
        else:
            # call LiteLLM SDK
            logger.debug(f"Routing embedding request for model={model} to liteLLM")
            return litellm.embedding(model=model, input=input, api_key=api_key, **kwargs)

    @wraps(orig_embeddings_async)
    async def patched_embeddings_async(self, *args, model=None, input=None, **kwargs):
        _, provider, api_key, _ = llm_utils.get_llm_provider(model)
        if provider == "openai":
            return await orig_embeddings_async(self, *args, model=model, input=input, **kwargs)
        else:
            logger.debug(f"Routing async embedding request for model={model} to liteLLM")
            return await litellm.aembedding(model=model, input=input, api_key=api_key, **kwargs)


    # Patch only the appropriate method based on client type
    if is_async:
        AsyncCompletions.create = patched_completions_async
        AsyncEmbeddings.create = patched_embeddings_async
    else:
        Completions.create = patched_completions_sync
        Embeddings.create = patched_embeddings_sync

    return client
