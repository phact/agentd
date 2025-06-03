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

from agentd.tool_decorator import SCHEMA_REGISTRY, FUNCTION_REGISTRY

_SERVER_CACHE = {}
logger = logging.getLogger(__name__)

async def _ensure_connected(server):
    """Cache-connected MCP servers so we only connect once per named server."""
    if server.name not in _SERVER_CACHE:
        await server.connect()
        _SERVER_CACHE[server.name] = server
    return _SERVER_CACHE[server.name]

def _run_async(coro):
    """Run an async coroutine from sync context."""
    return asyncio.new_event_loop().run_until_complete(coro)

def patch_openai_with_mcp(client):
    """
    Monkey-patch both sync and async chat.completions.create to:
      - accept explicit tools=[…], mcp_servers=[…], mcp_strict flag
      - fetch tools via MCPUtil.get_all_function_tools
      - merge explicit, MCP, and decorator @tool schemas
      - resolve collisions (explicit > MCP > decorator)
      - invoke MCP tools if requested, otherwise run local @tool functions
      - support OpenAI vs LiteLLM providers seamlessly
    """
    is_async = client.__class__.__name__ == 'AsyncOpenAI'

    # Keep references to the original methods
    orig_completions_sync = Completions.create
    orig_completions_async = AsyncCompletions.create
    orig_embeddings_sync = Embeddings.create
    orig_embeddings_async = AsyncEmbeddings.create

    async def _prepare_mcp_tools(servers, strict):
        """
        Connect to each MCP server (via _ensure_connected),
        call MCPUtil.get_all_function_tools on the list of connected servers,
        and return a list of JSON-Schema dicts in the format OpenAI expects.
        """
        connected = [await _ensure_connected(s) for s in servers]
        tool_objs = await MCPUtil.get_all_function_tools(connected, strict)
        mcp_schema_list = []
        for t in tool_objs:
            mcp_schema_list.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.params_json_schema
                }
            })
        return mcp_schema_list

    def _clean_kwargs(kwargs):
        """Strip out custom arguments so OpenAI/LiteLLM doesn’t complain."""
        cleaned = kwargs.copy()
        cleaned.pop('mcp_servers', None)
        cleaned.pop('mcp_strict', None)
        return cleaned

    MAX_TOOL_LOOPS = 20  # safety cap for recursive tool calls

    async def _handle_completion(
            self, args, model, messages,
            mcp_servers, mcp_strict, tools, kwargs, async_mode
    ):
        # Build the three separate “buckets” of schemas (explicit, MCP, decorator)
        explicit_schema_list = tools or []  # if the user passed tools=[…], take it as-is
        mcp_schema_list = []
        decorator_schema_list = list(SCHEMA_REGISTRY.values())

        #  If MCP servers were provided (and explicit tools is None),
        #    fetch MCP schemas now
        if mcp_servers is not None:
            mcp_schema_list = await _prepare_mcp_tools(mcp_servers, mcp_strict)

        #  If explicit tools were provided but they also passed mcp_servers (rare),
        #     we still want to merge: so fetch MCP schemas as well.
        #     (Above we only have an error if both mcp_servers and tools are non-None,
        #     so to actually allow all three merging, we COMMENT OUT the error above
        #     or handle it separately if we truly want to combine explicit + MCP + decorator.)
        #
        # For the requirement "merge all three together," we modify step 1 to:
        # if tools is not None and mcp_servers is not None:
        #     # Instead of error, proceed to fetch MCP and merge below
        #     pass
        #
        # But if you still want to forbid explicit AND MCP in the same call,
        # keep the ValueError. Here, we assume merging is allowed, so remove the ValueError:
        #
        #     # if mcp_servers is not None and tools is not None:
        #     #     raise ValueError(...)
        #
        # Then fetch MCP even if tools is set:
        #
        # mcp_schema_list = await _prepare_mcp_tools(mcp_servers, mcp_strict) if mcp_servers else []
        #
        # We’ve implemented that logic here.

        # Concatenate all three buckets in priority order: explicit → MCP → decorator
        #    (explicit_schema_list may be empty list if no explicit tools provided)
        explicit_tool_names = {schema["function"]["name"] for schema in explicit_schema_list}

        combined = explicit_schema_list + mcp_schema_list + decorator_schema_list

        # Deduplicate by function name, keeping first‐seen (explicit wins → MCP wins → decorator)
        final_tools_dict = {}
        for schema in combined:
            fname = schema["function"]["name"]
            if fname not in final_tools_dict:
                final_tools_dict[fname] = schema
            else:
                # Collision detected. We silently ignore duplicates because
                # earlier entries in `combined` have higher priority.
                logger.debug(f"Ignoring duplicate tool schema for name '{fname}'")
        final_tools = list(final_tools_dict.values())

        # Build a lookup from tool name → MCP server (if any)
        server_lookup = {}
        if mcp_servers:
            for srv in mcp_servers:
                # Each server.list_tools() gives us tool objects with `name`
                tool_list = await srv.list_tools()
                for t in tool_list:
                    server_lookup[t.name] = await _ensure_connected(srv)

        # Determine the LLM provider (OpenAI vs LiteLLM) for embeddings/completions
        _, provider, api_key, _ = llm_utils.get_llm_provider(model)
        clean_kwargs = _clean_kwargs(kwargs)

        # If we have at least one function schema, force tool_choice="auto" unless overridden
        if final_tools and "tool_choice" not in clean_kwargs:
            clean_kwargs["tool_choice"] = "auto"

        # Loop, letting the model call tools until no more `tool_calls` appear
        loop_count = 0
        current_messages = messages

        while True:
            loop_count += 1

            #  Make the chat completion call with the merged `final_tools`
            if provider == "openai":
                response = (
                    await orig_completions_async(self, *args,
                                                 model=model,
                                                 messages=current_messages,
                                                 tools=final_tools,
                                                 **clean_kwargs)
                    if async_mode else
                    orig_completions_sync(self, *args,
                                          model=model,
                                          messages=current_messages,
                                          tools=final_tools,
                                          **clean_kwargs)
                )
            else:
                response = (
                    await litellm.acompletion(model=model,
                                              messages=current_messages,
                                              tools=final_tools,
                                              api_key=api_key,
                                              **clean_kwargs)
                    if async_mode else
                    litellm.completion(model=model,
                                       messages=current_messages,
                                       tools=final_tools,
                                       api_key=api_key,
                                       **clean_kwargs)
                )

            #  Extract tool_calls from the model’s response
            if provider == "openai":
                tool_calls = response.choices[0].message.tool_calls
            else:
                tool_calls = getattr(response["choices"][0]["message"], "tool_calls", [])

            #  If no tool_calls or we hit the max loop count, return the response
            if not tool_calls or loop_count >= MAX_TOOL_LOOPS:
                if loop_count >= MAX_TOOL_LOOPS:
                    logger.warning(f"Reached max tool loops ({MAX_TOOL_LOOPS})")
                return response

            #  Otherwise, handle each tool_call
            for call in tool_calls:
                # Extract name and arguments robustly (OpenAI vs LiteLLM differences)
                if provider == "openai":
                    fn_name = call.function.name
                    fn_args = call.function.arguments
                else:
                    fn_name = call["function"]["name"]
                    fn_args = call["function"]["arguments"]

                if not isinstance(fn_args, dict):
                    fn_args = json.loads(fn_args)


                # If this name is in explicit_tool_names—and *not* in MCP or decorator—
                # then we bail out and return the response directly to the caller.
                if fn_name in explicit_tool_names and fn_name not in server_lookup and fn_name not in SCHEMA_REGISTRY:
                    # Don’t execute it locally. Instead, hand back the raw GPT response
                    # (which still contains call.function.arguments) so the caller can run it.
                    return response

                # Route to MCP or local @tool
                if fn_name in server_lookup:
                    # → MCP‐backed tool
                    server = server_lookup[fn_name]
                    logger.info(f"Invoking MCP tool '{fn_name}' with args {fn_args}")
                    result_obj = await server.call_tool(fn_name, fn_args)
                    tool_output = result_obj.dict().get("content")
                else:
                    # → local decorator‐registered function
                    logger.info(f"Invoking local @tool function '{fn_name}' with args {fn_args}")
                    fn = FUNCTION_REGISTRY.get(fn_name)
                    if fn is None:
                        raise KeyError(f"Tool '{fn_name}' not registered")
                    tool_output = fn(**fn_args)

                # Append the tool_call record and the tool’s output to the conversation
                #     in the format OpenAI expects
                if provider == "openai":
                    current_messages = current_messages + [
                        {"role": "assistant", "tool_calls": [call]},
                        {"role": "tool", "name": fn_name,
                         "content": str(tool_output),
                         "tool_call_id": call.id}
                    ]
                else:
                    call_id = call.get("id", None)
                    current_messages = current_messages + [
                        {"role": "assistant", "tool_calls": [call]},
                        {"role": "tool", "name": fn_name,
                         "content": str(tool_output),
                         "tool_call_id": call_id}
                    ]

                # Clear out `tools` and `tool_choice` for any subsequent loops
                clean_kwargs.pop("tools", None)
                clean_kwargs.pop("tool_choice", None)
                final_tools = None  # don’t resend schemas again

    @wraps(Completions.create)
    def patched_completions_sync(self, *args, model=None, messages=None,
                                 mcp_servers=None, mcp_strict=False,
                                 tools=None, **kwargs):
        return _run_async(_handle_completion(
            self, args, model, messages,
            mcp_servers, mcp_strict, tools, kwargs, False
        ))

    @wraps(AsyncCompletions.create)
    async def patched_completions_async(self, *args, model=None, messages=None,
                                        mcp_servers=None, mcp_strict=False,
                                        tools=None, **kwargs):
        return await _handle_completion(
            self, args, model, messages,
            mcp_servers, mcp_strict, tools, kwargs, True
        )

    @wraps(orig_embeddings_sync)
    def patched_embeddings_sync(self, *args, model=None, input=None, **kwargs):
        _, provider, api_key, _ = llm_utils.get_llm_provider(model)
        if provider == "openai":
            return orig_embeddings_sync(self, *args, model=model, input=input, **kwargs)
        else:
            logger.debug(f"Routing embedding request for model={model} to LiteLLM")
            return litellm.embedding(model=model, input=input, api_key=api_key, **kwargs)

    @wraps(orig_embeddings_async)
    async def patched_embeddings_async(self, *args, model=None, input=None, **kwargs):
        _, provider, api_key, _ = llm_utils.get_llm_provider(model)
        if provider == "openai":
            return await orig_embeddings_async(self, *args, model=model, input=input, **kwargs)
        else:
            logger.debug(f"Routing async embedding request for model={model} to LiteLLM")
            return await litellm.aembedding(model=model, input=input, api_key=api_key, **kwargs)

    # Patch the client based on sync vs. async
    if is_async:
        AsyncCompletions.create = patched_completions_async
        AsyncEmbeddings.create = patched_embeddings_async
    else:
        Completions.create = patched_completions_sync
        Embeddings.create = patched_embeddings_sync

    return client
