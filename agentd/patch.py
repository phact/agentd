import asyncio
import json
import logging
import types
from functools import wraps

from openai.resources.chat.completions import Completions, AsyncCompletions
from openai.resources.responses import Responses, AsyncResponses
from openai.resources.embeddings import Embeddings, AsyncEmbeddings
from openai.types.responses import (
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionCallArgumentsDoneEvent,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseFunctionToolCall
)

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
        try:
            tool_objs = await MCPUtil.get_all_function_tools(connected, strict, None, None)
        except TypeError:
            tool_objs = await MCPUtil.get_all_function_tools(connected, strict)
        schemas = []
        for t in tool_objs:
            schemas.append({
                "name": t.name,
                "description": t.description,
                "parameters": t.params_json_schema
            })
        return schemas

    def _should_stream_tool_results(kwargs):
        """Check if tool_call.results streaming is requested."""
        include = kwargs.get('include')
        return include and isinstance(include, list) and "tool_call.results" in include

    def _clean_kwargs(kwargs):
        cleaned = kwargs.copy()
        cleaned.pop('mcp_servers', None)
        cleaned.pop('mcp_strict', None)
        
        # Only remove "tool_call.results" from include list, preserve other include values
        include = kwargs.get('include')
        if include and isinstance(include, list):
            filtered_include = [item for item in include if item != "tool_call.results"]
            if filtered_include:
                cleaned['include'] = filtered_include
            else:
                cleaned.pop('include', None)
        
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
            is_responses=False, is_streaming=False
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

            # Handle streaming responses
            if is_streaming:
                stream_result = await _handle_streaming_responses(
                    self, args, model, input_history, final_tools, clean_kwargs,
                    provider, api_key, async_mode, orig_fn_sync, orig_fn_async,
                    server_lookup, kwargs.get('include')
                )
                if stream_result is not None:
                    return stream_result
                # If None returned, continue with non-streaming logic below

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

            # If streaming was requested, synthesize tool call events to show what tools are being executed
            if is_streaming:
                # Execute tools first (avoiding event loop conflicts)
                tasks = [
                    _execute_tool(call.name,
                                  json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments,
                                  server_lookup)
                    for call in calls
                ]
                results = await asyncio.gather(*tasks)
                
                # Build follow-up input with tool outputs
                follow_input = input_history[:]
                for call, result in zip(calls, results):
                    follow_input.append({
                        'type': 'function_call_output',
                        'call_id': call.call_id,
                        'output': json.dumps(result) if not isinstance(result, str) else result
                    })
                
                # Create a generator that yields tool call events followed by the final response
                def create_streaming_with_tool_events():
                    # Create proper event objects that match OpenAI format
                    class MockResponseOutputItemAddedEvent:
                        def __init__(self, item, output_index, sequence_number):
                            self.item = item
                            self.output_index = output_index
                            self.type = 'response.output_item.added'
                            self.sequence_number = sequence_number
                    
                    class MockResponseOutputItemDoneEvent:
                        def __init__(self, item, output_index, sequence_number):
                            self.item = item
                            self.output_index = output_index
                            self.type = 'response.output_item.done'
                            self.sequence_number = sequence_number
                    
                    class MockResponseFunctionToolCall:
                        def __init__(self, name, arguments, call_id, fc_id, status='completed'):
                            self.name = name
                            self.arguments = arguments
                            self.call_id = call_id
                            self.type = 'function_call'
                            self.id = fc_id
                            self.status = status
                    
                    # First yield tool call events to show what tools were executed
                    seq_num = 0
                    for i, call in enumerate(calls):
                        # Create a mock tool call object with result in a comment-like format
                        result_str = str(results[i])
                        mock_call = MockResponseFunctionToolCall(
                            name=call.name,
                            arguments=call.arguments,
                            call_id=call.call_id,
                            fc_id=getattr(call, 'id', f'fc_{call.call_id}'),
                            status='completed'
                        )
                        
                        # Add result as a custom attribute for visibility
                        mock_call.tool_result = result_str
                        
                        # Yield output item added event
                        yield MockResponseOutputItemAddedEvent(
                            item=mock_call,
                            output_index=i,
                            sequence_number=seq_num
                        )
                        seq_num += 1
                        
                        # Yield output item done event
                        yield MockResponseOutputItemDoneEvent(
                            item=mock_call,
                            output_index=i,
                            sequence_number=seq_num
                        )
                        seq_num += 1
                    
                    # Make streaming follow-up call
                    follow_kwargs = clean_kwargs.copy()
                    follow_kwargs.pop('previous_response_id', None)
                    follow_kwargs['stream'] = True
                    
                    if provider == 'openai':
                        if async_mode:
                            # Can't use _run_async here due to loop conflicts
                            # This will only work for async mode
                            follow_stream = orig_fn_async(
                                self, *args,
                                model=model,
                                input=follow_input,
                                previous_response_id=resp.id,
                                **follow_kwargs
                            )
                        else:
                            follow_stream = orig_fn_sync(
                                self, *args,
                                model=model,
                                input=follow_input,
                                previous_response_id=resp.id,
                                **follow_kwargs
                            )
                    else:
                        if async_mode:
                            follow_stream = litellm.aresponses(
                                model=model,
                                input=follow_input,
                                previous_response_id=resp.id,
                                api_key=api_key,
                                **follow_kwargs
                            )
                        else:
                            follow_stream = litellm.responses(
                                model=model,
                                input=follow_input,
                                previous_response_id=resp.id,
                                api_key=api_key,
                                **follow_kwargs
                            )
                    
                    # Stream the follow-up response
                    if async_mode:
                        # For async mode, we need to handle the async stream properly
                        for event in follow_stream:
                            yield event
                    else:
                        # For sync mode, the stream should be sync
                        for event in follow_stream:
                            yield event
                
                if async_mode:
                    # For async mode, we can handle the async generator properly
                    async def async_streaming_with_tool_events():
                        # Create proper event objects that match OpenAI format
                        class MockResponseOutputItemAddedEvent:
                            def __init__(self, item, output_index, sequence_number):
                                self.item = item
                                self.output_index = output_index
                                self.type = 'response.output_item.added'
                                self.sequence_number = sequence_number
                        
                        class MockResponseOutputItemDoneEvent:
                            def __init__(self, item, output_index, sequence_number):
                                self.item = item
                                self.output_index = output_index
                                self.type = 'response.output_item.done'
                                self.sequence_number = sequence_number
                        
                        class MockResponseFunctionToolCall:
                            def __init__(self, name, arguments, call_id, fc_id, status='completed'):
                                self.name = name
                                self.arguments = arguments
                                self.call_id = call_id
                                self.type = 'function_call'
                                self.id = fc_id
                                self.status = status
                        
                        # First yield tool call events to show what tools were executed
                        seq_num = 0
                        for i, call in enumerate(calls):
                            # Create a mock tool call object with result in a comment-like format
                            result_str = str(results[i])
                            mock_call = MockResponseFunctionToolCall(
                                name=call.name,
                                arguments=call.arguments,
                                call_id=call.call_id,
                                fc_id=getattr(call, 'id', f'fc_{call.call_id}'),
                                status='completed'
                            )
                            
                            # Add result as a custom attribute for visibility
                            mock_call.tool_result = result_str
                            
                            # Yield output item added event
                            yield MockResponseOutputItemAddedEvent(
                                item=mock_call,
                                output_index=i,
                                sequence_number=seq_num
                            )
                            seq_num += 1
                            
                            # Yield output item done event
                            yield MockResponseOutputItemDoneEvent(
                                item=mock_call,
                                output_index=i,
                                sequence_number=seq_num
                            )
                            seq_num += 1
                        
                        # Make streaming follow-up call
                        follow_kwargs = clean_kwargs.copy()
                        follow_kwargs.pop('previous_response_id', None)
                        follow_kwargs['stream'] = True
                        
                        if provider == 'openai':
                            follow_stream = await orig_fn_async(
                                self, *args,
                                model=model,
                                input=follow_input,
                                previous_response_id=resp.id,
                                **follow_kwargs
                            )
                        else:
                            follow_stream = await litellm.aresponses(
                                model=model,
                                input=follow_input,
                                previous_response_id=resp.id,
                                api_key=api_key,
                                **follow_kwargs
                            )
                        
                        # Stream the follow-up response
                        async for event in follow_stream:
                            yield event
                    
                    return async_streaming_with_tool_events()
                else:
                    return create_streaming_with_tool_events()

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
            
            # Re-enable streaming for the follow-up call if it was originally requested
            if is_streaming:
                follow_kwargs['stream'] = True

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

    async def _handle_streaming_responses(
            self, args, model, input_history, final_tools, clean_kwargs,
            provider, api_key, async_mode, orig_fn_sync, orig_fn_async,
            server_lookup, include=None
    ):
        """Handle streaming responses with automatic tool call execution."""
        
        async def stream_with_tool_handling():
            # 1) Initial streaming call
            if provider == 'openai':
                if async_mode:
                    stream = await orig_fn_async(
                        self, *args,
                        model=model,
                        input=input_history,
                        tools=final_tools,
                        **clean_kwargs
                    )
                else:
                    stream = orig_fn_sync(
                        self, *args,
                        model=model,
                        input=input_history,
                        tools=final_tools,
                        **clean_kwargs
                    )
            else:
                if async_mode:
                    stream = await litellm.aresponses(
                        model=model,
                        input=input_history,
                        tools=final_tools,
                        api_key=api_key,
                        **clean_kwargs
                    )
                else:
                    stream = litellm.responses(
                        model=model,
                        input=input_history,
                        tools=final_tools,
                        api_key=api_key,
                        **clean_kwargs
                    )
            
            response_id = None
            
            if async_mode:
                async for event in stream:
                    event_type = getattr(event, 'type', None)
                    
                    if event_type == 'response.created':
                        response_id = getattr(event.response, 'id', None) if hasattr(event, 'response') else None
                        yield event
                        
                    elif event_type == 'response.completed':
                        # Response complete - check if function calls are in the completed response
                        response_obj = getattr(event, 'response', None)
                        if response_obj and hasattr(response_obj, 'output'):
                            tool_calls = [
                                item for item in response_obj.output 
                                if getattr(item, 'type', None) == 'function_call'
                            ]
                            
                            if tool_calls:
                                # Execute all function calls in parallel
                                tasks = []
                                for call in tool_calls:
                                    fn_name = getattr(call, 'name', None)
                                    fn_args = getattr(call, 'arguments', '{}')
                                    if isinstance(fn_args, str):
                                        fn_args = json.loads(fn_args)
                                    tasks.append(_execute_tool(fn_name, fn_args, server_lookup))
                                
                                results = await asyncio.gather(*tasks)
                                
                                # Check if we should emit tool call streaming events
                                should_stream_tools = include and isinstance(include, list) and "tool_call.results" in include
                                print(f"[ASYNC] Should stream tool results: {should_stream_tools}")
                                
                                # Build follow-up input with tool outputs
                                follow_input = input_history[:]
                                for i, (call, result) in enumerate(zip(tool_calls, results)):
                                    call_id = getattr(call, 'call_id', None)
                                    call_alt_id = getattr(call, 'id', None)
                                    fn_name = getattr(call, 'name', None)
                                    fn_args = getattr(call, 'arguments', '{}')
                                    
                                    print(f"[ASYNC] Processing tool call - name: {fn_name}, call_id: {call_id}, id: {call_alt_id}, type: {type(call)}")
                                    print(f"[ASYNC] Call object attributes: {[attr for attr in dir(call) if not attr.startswith('_')]}")
                                    
                                    if should_stream_tools:
                                        print(f"[ASYNC] Emitting streaming events for tool call {i+1}")
                                        # Emit tool call streaming events
                                        tool_call_item = ResponseFunctionToolCall(
                                            arguments='',
                                            call_id=call_id,
                                            name=fn_name,
                                            type='function_call',
                                            id=call_alt_id or f'fc_{i+1}',
                                            status='in_progress'
                                        )
                                        
                                        # Emit output item added event
                                        yield ResponseOutputItemAddedEvent(
                                            item=tool_call_item,
                                            output_index=i,
                                            sequence_number=i * 4,
                                            type='response.output_item.added'
                                        )
                                        
                                        # Emit arguments delta event
                                        yield ResponseFunctionCallArgumentsDeltaEvent(
                                            delta=fn_args,
                                            item_id=call_alt_id or f'fc_{i+1}',
                                            output_index=i,
                                            sequence_number=i * 4 + 1,
                                            type='response.function_call_arguments.delta'
                                        )
                                        
                                        # Emit arguments done event
                                        yield ResponseFunctionCallArgumentsDoneEvent(
                                            arguments=fn_args,
                                            item_id=call_alt_id or f'fc_{i+1}',
                                            output_index=i,
                                            sequence_number=i * 4 + 2,
                                            type='response.function_call_arguments.done'
                                        )
                                        
                                        # Create completed tool call with results
                                        completed_tool_call = ResponseFunctionToolCall(
                                            arguments=fn_args,
                                            call_id=call_id,
                                            name=fn_name,
                                            type='function_call',
                                            id=call_alt_id or f'fc_{i+1}',
                                            status='completed'
                                        )
                                        
                                        # Add custom attributes for tool results (like in your example)
                                        completed_tool_call.inputs = json.loads(fn_args) if isinstance(fn_args, str) else fn_args
                                        completed_tool_call.tool_name = fn_name
                                        completed_tool_call.results = result if isinstance(result, list) else [result]
                                        
                                        # Emit output item done event with results
                                        yield ResponseOutputItemDoneEvent(
                                            item=completed_tool_call,
                                            output_index=i,
                                            sequence_number=i * 4 + 3,
                                            type='response.output_item.done'
                                        )
                                    
                                    follow_input.append({
                                        'type': 'function_call_output',
                                        'call_id': call_id,
                                        'output': json.dumps(result) if not isinstance(result, str) else result
                                    })
                                    print(f"[ASYNC] Added function_call_output with call_id: {call_id}")
                                
                                print(f"[ASYNC] Follow-up input: {follow_input}")
                                print(f"[ASYNC] Input history type: {type(input_history)}, value: {input_history}")
                                
                                # Make follow-up streaming call
                                follow_kwargs = clean_kwargs.copy()
                                # Keep previous_response_id for call_id context
                                # follow_kwargs.pop('previous_response_id', None)
                                
                                # Add a small delay to ensure the response is fully processed
                                await asyncio.sleep(0.1)
                                
                                if provider == 'openai':
                                    # Include previous_response_id for call_id context
                                    follow_stream = await orig_fn_async(
                                        self, *args,
                                        model=model,
                                        input=follow_input,
                                        previous_response_id=response_id,
                                        **follow_kwargs
                                    )
                                else:
                                    follow_stream = await litellm.aresponses(
                                        model=model,
                                        input=follow_input,
                                        previous_response_id=response_id,
                                        api_key=api_key,
                                        **follow_kwargs
                                    )
                                
                                # Stream the follow-up response
                                async for follow_event in follow_stream:
                                    yield follow_event
                            else:
                                # No function calls, just yield the final event
                                yield event
                        else:
                            # No function calls, just yield the final event
                            yield event
                    else:
                        # Pass through all other events
                        yield event
            else:
                for event in stream:
                    event_type = getattr(event, 'type', None)
                    
                    if event_type == 'response.created':
                        response_id = getattr(event.response, 'id', None) if hasattr(event, 'response') else None
                        yield event
                        
                    elif event_type == 'response.completed':
                        # Response complete - check if function calls are in the completed response
                        response_obj = getattr(event, 'response', None)
                        if response_obj and hasattr(response_obj, 'output'):
                            tool_calls = [
                                item for item in response_obj.output 
                                if getattr(item, 'type', None) == 'function_call'
                            ]
                            
                            if tool_calls:
                                # Execute all function calls in parallel
                                tasks = []
                                for call in tool_calls:
                                    fn_name = getattr(call, 'name', None)
                                    fn_args = getattr(call, 'arguments', '{}')
                                    if isinstance(fn_args, str):
                                        fn_args = json.loads(fn_args)
                                    tasks.append(_execute_tool(fn_name, fn_args, server_lookup))
                                
                                results = await asyncio.gather(*tasks)
                                
                                # Check if we should emit tool call streaming events
                                should_stream_tools = include and isinstance(include, list) and "tool_call.results" in include
                                print(f"[SYNC] Should stream tool results: {should_stream_tools}")
                                
                                # Build follow-up input with tool outputs
                                follow_input = input_history[:]
                                for i, (call, result) in enumerate(zip(tool_calls, results)):
                                    call_id = getattr(call, 'call_id', None)
                                    call_alt_id = getattr(call, 'id', None)
                                    fn_name = getattr(call, 'name', None)
                                    fn_args = getattr(call, 'arguments', '{}')
                                    
                                    print(f"[SYNC] Processing tool call - name: {fn_name}, call_id: {call_id}, id: {call_alt_id}")
                                    
                                    if should_stream_tools:
                                        print(f"[SYNC] Emitting streaming events for tool call {i+1}")
                                        # Emit tool call streaming events
                                        tool_call_item = ResponseFunctionToolCall(
                                            arguments='',
                                            call_id=call_id,
                                            name=fn_name,
                                            type='function_call',
                                            id=call_alt_id or f'fc_{i+1}',
                                            status='in_progress'
                                        )
                                        
                                        # Emit output item added event
                                        yield ResponseOutputItemAddedEvent(
                                            item=tool_call_item,
                                            output_index=i,
                                            sequence_number=i * 4,
                                            type='response.output_item.added'
                                        )
                                        
                                        # Emit arguments delta event
                                        yield ResponseFunctionCallArgumentsDeltaEvent(
                                            delta=fn_args,
                                            item_id=call_alt_id or f'fc_{i+1}',
                                            output_index=i,
                                            sequence_number=i * 4 + 1,
                                            type='response.function_call_arguments.delta'
                                        )
                                        
                                        # Emit arguments done event
                                        yield ResponseFunctionCallArgumentsDoneEvent(
                                            arguments=fn_args,
                                            item_id=call_alt_id or f'fc_{i+1}',
                                            output_index=i,
                                            sequence_number=i * 4 + 2,
                                            type='response.function_call_arguments.done'
                                        )
                                        
                                        # Create completed tool call with results
                                        completed_tool_call = ResponseFunctionToolCall(
                                            arguments=fn_args,
                                            call_id=call_id,
                                            name=fn_name,
                                            type='function_call',
                                            id=call_alt_id or f'fc_{i+1}',
                                            status='completed'
                                        )
                                        
                                        # Add custom attributes for tool results (like in your example)
                                        completed_tool_call.inputs = json.loads(fn_args) if isinstance(fn_args, str) else fn_args
                                        completed_tool_call.tool_name = fn_name
                                        completed_tool_call.results = result if isinstance(result, list) else [result]
                                        
                                        # Emit output item done event with results
                                        yield ResponseOutputItemDoneEvent(
                                            item=completed_tool_call,
                                            output_index=i,
                                            sequence_number=i * 4 + 3,
                                            type='response.output_item.done'
                                        )
                                    
                                    follow_input.append({
                                        'type': 'function_call_output',
                                        'call_id': call_id,
                                        'output': json.dumps(result) if not isinstance(result, str) else result
                                    })
                                    print(f"[SYNC] Added function_call_output with call_id: {call_id}")
                                
                                # Make follow-up streaming call
                                follow_kwargs = clean_kwargs.copy()
                                follow_kwargs.pop('previous_response_id', None)
                                
                                if provider == 'openai':
                                    follow_stream = orig_fn_sync(
                                        self, *args,
                                        model=model,
                                        input=follow_input,
                                        previous_response_id=response_id,
                                        **follow_kwargs
                                    )
                                else:
                                    follow_stream = litellm.responses(
                                        model=model,
                                        input=follow_input,
                                        previous_response_id=response_id,
                                        api_key=api_key,
                                        **follow_kwargs
                                    )
                                
                                # Stream the follow-up response
                                for follow_event in follow_stream:
                                    yield follow_event
                            else:
                                # No function calls, just yield the final event
                                yield event
                        else:
                            # No function calls, just yield the final event
                            yield event
                    else:
                        # Pass through all other events
                        yield event
        
        if async_mode:
            return stream_with_tool_handling()
        else:
            # For sync mode, streaming tool calls are complex due to event loop requirements
            # For now, fall back to non-streaming behavior to avoid MCP server conflicts
            logger.warning("Streaming with tool calls not supported in sync mode, falling back to non-streaming")
            clean_kwargs.pop('stream', None)
            # Fall through to non-streaming handling below
            return None  # Signal to continue with non-streaming logic

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
                               tools=None, stream=False, include=None, **kwargs):
        kwargs['stream'] = stream
        kwargs['include'] = include
        return _run_async(_handle_llm_call(
            self, args, model, input,
            mcp_servers, mcp_strict, tools, kwargs,
            False, orig_responses_sync, orig_responses_async, True, stream
        ))

    @wraps(orig_responses_async)
    async def patched_responses_async(self, *args, model=None, input=None,
                                      mcp_servers=None, mcp_strict=False,
                                      tools=None, stream=False, include=None, **kwargs):
        kwargs['stream'] = stream
        kwargs['include'] = include
        return await _handle_llm_call(
            self, args, model, input,
            mcp_servers, mcp_strict, tools, kwargs,
            True, orig_responses_sync, orig_responses_async, True, stream
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
