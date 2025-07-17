#!/usr/bin/env python3

import json
import asyncio
from openai import AsyncOpenAI
from agentd.tool_decorator import tool

@tool
def add_numbers(x: int, y: int) -> int:
    """
    Add two integers.

    x: first addend
    y: second addend
    """
    return x + y

@tool
def greet(name: str) -> str:
    """
    Return a greeting.

    name: person's name
    """
    return f"Hello, {name}!"

async def test_manual_tool_execution():
    """
    Manual tool execution with unpatched client to understand the flow.
    """
    client = AsyncOpenAI()  # No patch!
    print("\n=== Manual Tool Execution (Baseline) ===")
    
    input_msg = "What is add_numbers(10, 5)? Then greet 'Alice'."
    
    # Create explicit tool schemas for Responses API format
    tools = [
        {
            "type": "function",
            "name": "add_numbers",
            "description": "Add two integers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "first addend"},
                    "y": {"type": "integer", "description": "second addend"}
                },
                "required": ["x", "y"]
            }
        },
        {
            "type": "function", 
            "name": "greet",
            "description": "Return a greeting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "person's name"}
                },
                "required": ["name"]
            }
        }
    ]
    
    # Step 1: Initial call
    print("Step 1: Initial call")
    response = await client.responses.create(
        model="gpt-4o-mini",
        input=input_msg,
        tools=tools,
        stream=False  # Non-streaming first
    )
    
    print(f"Initial response ID: {response.id}")
    print(f"Initial response status: {response.status}")
    print(f"Initial response output: {response.output}")
    
    # Step 2: Extract function calls
    function_calls = [item for item in response.output if item.type == 'function_call']
    print(f"\nStep 2: Found {len(function_calls)} function calls")
    
    # Step 3: Execute function calls manually
    print("\nStep 3: Execute function calls manually")
    function_outputs = []
    
    for call in function_calls:
        print(f"  Executing: {call.name} with args {call.arguments}")
        
        # Parse arguments
        args = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
        
        # Execute the function
        if call.name == "add_numbers":
            result = add_numbers(**args)
        elif call.name == "greet":
            result = greet(**args)
        else:
            result = f"Unknown function: {call.name}"
        
        print(f"  Result: {result}")
        
        # Create function output
        function_outputs.append({
            "type": "function_call_output",
            "call_id": call.call_id,
            "output": str(result)
        })
    
    # Step 4: Make follow-up call with function outputs
    print("\nStep 4: Follow-up call with function outputs")
    follow_up_input = [
        {"role": "user", "content": input_msg}
    ] + function_outputs
    
    print(f"Follow-up input: {follow_up_input}")
    
    follow_up_response = await client.responses.create(
        model="gpt-4o-mini",
        input=follow_up_input,
        previous_response_id=response.id,
        stream=False
    )
    
    print(f"Follow-up response: {follow_up_response.output}")
    
    # Step 5: Try the same thing with streaming
    print("\n" + "="*50)
    print("Step 5: Now try with streaming")
    
    # Initial streaming call
    stream = await client.responses.create(
        model="gpt-4o-mini",
        input=input_msg,
        tools=tools,
        stream=True
    )
    
    print("Initial streaming response:")
    response_obj = None
    async for event in stream:
        print(f"  {event.type}: {event}")
        if event.type == 'response.completed':
            response_obj = event.response
    
    # Extract function calls from streaming response
    if response_obj and response_obj.output:
        function_calls = [item for item in response_obj.output if item.type == 'function_call']
        print(f"Found {len(function_calls)} function calls in streaming response")
        
        # Execute function calls manually (same as before)
        function_outputs = []
        for call in function_calls:
            print(f"  Executing: {call.name} with args {call.arguments}")
            args = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
            
            if call.name == "add_numbers":
                result = add_numbers(**args)
            elif call.name == "greet":
                result = greet(**args)
            else:
                result = f"Unknown function: {call.name}"
            
            print(f"  Result: {result}")
            function_outputs.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": str(result)
            })
        
        # Follow-up streaming call
        print("\nFollow-up streaming call:")
        follow_up_input = [
            {"role": "user", "content": input_msg}
        ] + function_outputs
        
        follow_up_stream = await client.responses.create(
            model="gpt-4o-mini",
            input=follow_up_input,
            previous_response_id=response_obj.id,  # Need this for call_id context
            stream=True
        )
        
        print("Follow-up streaming response:")
        async for event in follow_up_stream:
            print(f"  {event.type}: {event}")

if __name__ == "__main__":
    asyncio.run(test_manual_tool_execution())