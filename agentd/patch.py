import asyncio
import json
import logging
import types
from functools import wraps

from openai.resources.chat.completions import Completions, AsyncCompletions
from openai.resources.responses import Responses, AsyncResponses
from openai.resources.embeddings import Embeddings, AsyncEmbeddings

from agents.mcp.util import MCPUtil
import litellm.utils as llm_utils
import litellm

from agentd.tool_decorator import SCHEMA_REGISTRY, FUNCTION_REGISTRY

logger = logging.getLogger(__name__)

async def _ensure_connected(server, server_cache):
    """Cache-connected MCP servers so we only connect once per named server."""
    if server.name not in server_cache:
        await server.connect()
        server_cache[server.name] = server
    return server_cache[server.name]

def _run_async(coro):
    """Run an async coroutine from sync context."""
    return asyncio.new_event_loop().run_until_complete(coro)

def patch_openai_with_mcp(client):
    """
    Monkey-patch Completions, Responses, and Embeddings to integrate MCP tools,
    local @tool functions, and LiteLLM support.
    """
    is_async = client.__class__.__name__ == 'AsyncOpenAI'
    
    # Add per-client server cache
    client._mcp_server_cache = {}

    # Keep references to the original OpenAI SDK methods
    orig_completions_sync = Completions.create
    orig_completions_async = AsyncCompletions.create
    orig_responses_sync = Responses.create
    orig_responses_async = AsyncResponses.create
    orig_embeddings_sync = Embeddings.create
    orig_embeddings_async = AsyncEmbeddings.create

    async def _prepare_mcp_tools(servers, strict, server_cache):
        connected = [await _ensure_connected(s, server_cache) for s in servers]
        tool_objs = await MCPUtil.get_all_function_tools(connected, strict)
        schemas = []
        for t in tool_objs:
            schemas.append({
                "name": t.name,
                "description": t.description,
                "parameters": t.params_json_schema
            })
        return schemas

    def _clean_kwargs(kwargs):
        cleaned = kwargs.copy()
        cleaned.pop('mcp_servers', None)
        cleaned.pop('mcp_strict', None)
        return cleaned

    MAX_TOOL_LOOPS = 20

    def _normalize_schema(schema):
        # Flatten either dict with nested 'function' or flat dict
        if 'function' in schema:
            fn = schema['function']
            return {
                'name': fn.get('name'),
                'description': fn.get('description'),
                'parameters': fn.get('parameters') or fn.get('params_json_schema')
            }
        return {
            'name': schema.get('name'),
            'description': schema.get('description'),
            'parameters': schema.get('parameters') or schema.get('params_json_schema')
        }

    async def _process_tool_call(call, fn_name, fn_args, server_lookup, provider, is_responses=False):
        if fn_name in server_lookup:
            server = server_lookup[fn_name]
            logger.info(f"Invoking MCP tool '{fn_name}' with args {fn_args}")
            try:
                result = await server.call_tool(fn_name, fn_args)
                output = result.dict().get('content')
            except Exception as e:
                logger.error(f"MCP tool call failed: {e}")
                output = f"Error calling MCP tool {fn_name}: {str(e)}"
        else:
            logger.info(f"Invoking local @tool function '{fn_name}' with args {fn_args}")
            fn = FUNCTION_REGISTRY.get(fn_name)
            if fn is None:
                raise KeyError(f"Tool '{fn_name}' not registered")
            output = fn(**fn_args)
            if asyncio.iscoroutine(output):
                output = await output

        if is_responses:
            call_id = getattr(call, 'call_id', getattr(call, 'id', None))
            return {"type": "function_call_output", "call_id": call_id, "output": str(output)}

        # Completions path: inject back as chat messages
        call_id = getattr(call, 'id', None)
        return [
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "name": fn_name, "content": str(output), "tool_call_id": call_id}
        ]

    async def _handle_llm_call(
            self, args, model, payload,
            mcp_servers, mcp_strict, tools, kwargs,
            async_mode, orig_fn_sync, orig_fn_async,
            is_responses=False
    ):
        """
        Unified handler for both Chat Completions (is_responses=False)
        and Responses API (is_responses=True). Supports OpenAI and LiteLLM providers.
        """
        # 1) Gather tool schemas
        explicit = tools or []
        client_obj = getattr(self, '_client', None) or getattr(self, 'client', None)
        server_cache = getattr(client_obj, '_mcp_server_cache', {}) if client_obj else {}
        mcp_schemas = await _prepare_mcp_tools(mcp_servers, mcp_strict, server_cache) if mcp_servers else []
        decorator = list(SCHEMA_REGISTRY.values())
        combined = explicit + mcp_schemas + decorator
        # Deduplicate tools by normalized name
        deduped = {}
        for schema in combined:
            flat = _normalize_schema(schema)
            name = flat['name']
            if name and name not in deduped:
                deduped[name] = schema

        # 2) Build tool definitions
        final_tools = []
        for schema in deduped.values():
            flat = _normalize_schema(schema)
            if is_responses:
                final_tools.append({
                    'type': 'function',
                    'name': flat['name'],
                    'description': flat['description'],
                    'parameters': flat['parameters']
                })
            else:
                final_tools.append({'type': 'function', 'function': flat})

        # 3) Connect MCP servers
        server_lookup = {}
        for srv in mcp_servers or []:
            conn = await _ensure_connected(srv, server_cache)
            for t in await conn.list_tools():
                server_lookup[t.name] = conn

        # 4) Determine provider & clean kwargs
        _, provider, api_key, _ = llm_utils.get_llm_provider(model)
        clean_kwargs = _clean_kwargs(kwargs)
        if final_tools and 'tool_choice' not in clean_kwargs:
            clean_kwargs['tool_choice'] = 'auto'

        # === RESPONSES API ===
        if is_responses:
            # Ensure payload is a list of message dicts
            input_history = payload.copy() if isinstance(payload, list) else [{'role': 'user', 'content': str(payload)}]

            # 1) Initial call: let model emit any function_call messages
            if provider == 'openai':
                if async_mode:
                    resp = await orig_fn_async(
                        self, *args,
                        model=model,
                        input=input_history,
                        tools=final_tools,
                        **clean_kwargs
                    )
                else:
                    resp = orig_fn_sync(
                        self, *args,
                        model=model,
                        input=input_history,
                        tools=final_tools,
                        **clean_kwargs
                    )
            else:
                if async_mode:
                    resp = await litellm.aresponses(
                        model=model,
                        input=input_history,
                        tools=final_tools,
                        api_key=api_key,
                        **clean_kwargs
                    )
                else:
                    resp = litellm.responses(
                        model=model,
                        input=input_history,
                        tools=final_tools,
                        api_key=api_key,
                        **clean_kwargs
                    )

            # Extract all function calls
            calls = [o for o in getattr(resp, 'output', []) if getattr(o, 'type', None) == 'function_call']
            if not calls:
                return resp

            # Execute all tool calls in parallel
            tasks = [
                _execute_tool(call.name,
                              json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments,
                              server_lookup)
                for call in calls
            ]
            results = await asyncio.gather(*tasks)

            # Build follow-up input preserving full history
            follow_input = input_history
            for call, result in zip(calls, results):
                follow_input.append({
                    'type': 'function_call_output',
                    'call_id': call.call_id,
                    'output': json.dumps(result) if not isinstance(result, str) else result
                })

            # 2) Follow-up call with full history + tool outputs
            # Prepare follow-up kwargs, avoid duplicating previous_response_id
            follow_kwargs = clean_kwargs.copy()
            follow_kwargs.pop('previous_response_id', None)

            if provider == 'openai':
                if async_mode:
                    follow = await orig_fn_async(
                        self, *args,
                        model=model,
                        input=follow_input,
                        previous_response_id=resp.id,
                        **follow_kwargs
                    )
                else:
                    follow = orig_fn_sync(
                        self, *args,
                        model=model,
                        input=follow_input,
                        previous_response_id=resp.id,
                        **follow_kwargs
                    )
            else:
                if async_mode:
                    follow = await litellm.aresponses(
                        model=model,
                        input=follow_input,
                        previous_response_id=resp.id,
                        api_key=api_key,
                        **follow_kwargs
                    )
                else:
                    follow = litellm.responses(
                        model=model,
                        input=follow_input,
                        previous_response_id=resp.id,
                        api_key=api_key,
                        **follow_kwargs
                    )
            return follow

        # === CHAT COMPLETIONS: multi-call tool loop ===
        current_messages = payload
        loop_count = 0
        while True:
            loop_count += 1
            call_args = {'model': model, 'messages': current_messages, **clean_kwargs}
            if provider != 'openai':
                call_args['api_key'] = api_key
            if final_tools and 'tool_choice' in clean_kwargs:
                call_args['tools'] = final_tools

            if provider == 'openai':
                resp = await orig_fn_async(self, *args, **call_args) if async_mode else orig_fn_sync(self, *args, **call_args)
            else:
                resp = await litellm.acompletion(**call_args) if async_mode else litellm.completion(**call_args)

            tool_calls = (
                getattr(resp.choices[0].message, 'tool_calls', []) if provider == 'openai'
                else getattr(resp['choices'][0]['message'], 'tool_calls', [])
            )
            if not tool_calls or loop_count >= MAX_TOOL_LOOPS:
                if loop_count >= MAX_TOOL_LOOPS:
                    logger.warning(f"Reached max tool loops ({MAX_TOOL_LOOPS})")
                return resp

            tasks = []
            explicit_names = {s.get('name') for s in explicit if isinstance(s, dict)}
            for call in tool_calls:
                if provider == 'openai':
                    name, raw = call.function.name, call.function.arguments
                else:
                    name, raw = call['function']['name'], call['function']['arguments']
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if name in explicit_names and name not in server_lookup and name not in SCHEMA_REGISTRY:
                    return resp
                tasks.append(_process_tool_call(call, name, parsed, server_lookup, provider, False))

            parts = await asyncio.gather(*tasks)
            for part in parts:
                current_messages.extend(part)
            clean_kwargs.pop('tools', None)
            clean_kwargs.pop('tool_choice', None)
            final_tools = None

    # Helper to execute MCP or local tools
    async def _execute_tool(fn_name, fn_args, server_lookup):
        if fn_name in server_lookup:
            res = await server_lookup[fn_name].call_tool(fn_name, fn_args)
            return res.dict().get('content')
        fn = FUNCTION_REGISTRY.get(fn_name)
        out = fn(**fn_args)
        return await out if asyncio.iscoroutine(out) else out


    # Patch into the SDK
    @wraps(orig_completions_sync)
    def patched_completions_sync(self, *args, model=None, messages=None,
                                 mcp_servers=None, mcp_strict=False,
                                 tools=None, **kwargs):
        return _run_async(_handle_llm_call(
            self, args, model, messages,
            mcp_servers, mcp_strict, tools, kwargs,
            False, orig_completions_sync, orig_completions_async, False
        ))

    @wraps(orig_completions_async)
    async def patched_completions_async(self, *args, model=None, messages=None,
                                        mcp_servers=None, mcp_strict=False,
                                        tools=None, **kwargs):
        return await _handle_llm_call(
            self, args, model, messages,
            mcp_servers, mcp_strict, tools, kwargs,
            True, orig_completions_sync, orig_completions_async, False
        )

    @wraps(orig_responses_sync)
    def patched_responses_sync(self, *args, model=None, input=None,
                               mcp_servers=None, mcp_strict=False,
                               tools=None, **kwargs):
        return _run_async(_handle_llm_call(
            self, args, model, input,
            mcp_servers, mcp_strict, tools, kwargs,
            False, orig_responses_sync, orig_responses_async, True
        ))

    @wraps(orig_responses_async)
    async def patched_responses_async(self, *args, model=None, input=None,
                                      mcp_servers=None, mcp_strict=False,
                                      tools=None, **kwargs):
        return await _handle_llm_call(
            self, args, model, input,
            mcp_servers, mcp_strict, tools, kwargs,
            True, orig_responses_sync, orig_responses_async, True
        )

    @wraps(orig_embeddings_sync)
    def patched_embeddings_sync(self, *args, model=None, input=None, **kwargs):
        _, provider, api_key, _ = llm_utils.get_llm_provider(model)
        if provider == 'openai':
            return orig_embeddings_sync(self, *args, model=model, input=input, **kwargs)
        return litellm.embedding(model=model, input=input, api_key=api_key, **kwargs)

    @wraps(orig_embeddings_async)
    async def patched_embeddings_async(self, *args, model=None, input=None, **kwargs):
        _, provider, api_key, _ = llm_utils.get_llm_provider(model)
        if provider == 'openai':
            return await orig_embeddings_async(self, *args, model=model, input=input, **kwargs)
        return await litellm.aembedding(model=model, input=input, api_key=api_key, **kwargs)

    # Apply patches
    if is_async:
        client.chat.completions.create = types.MethodType(patched_completions_async, client.chat.completions)
        client.responses.create = types.MethodType(patched_responses_async, client.responses)
        client.embeddings.create = types.MethodType(patched_embeddings_async, client.embeddings)
    else:
        client.chat.completions.create = types.MethodType(patched_completions_sync, client.chat.completions)
        client.responses.create = types.MethodType(patched_responses_sync, client.responses)
        client.embeddings.create = types.MethodType(patched_embeddings_sync, client.embeddings)

    return client
