#!/usr/bin/env python3

import json
import os
import asyncio
from openai import OpenAI, AsyncOpenAI
from agentd.patch import patch_openai_with_mcp
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

def print_response(label, response):
    """Helper to print out content or function_call, whichever is present."""
    for chunk in response:
        print(f"{label}: {chunk}")
        if hasattr(chunk, '__dict__'):
            print(f"  Keys: {list(chunk.__dict__.keys())}")
            # Special handling for tool result visibility
            if hasattr(chunk, 'item') and hasattr(chunk.item, 'tool_result'):
                print(f"  Tool Result: {chunk.item.tool_result}")

async def print_response_async(label, response):
    """Helper to print out content or function_call for async responses."""
    async for chunk in response:
        print(f"{label}: {chunk}")
        if hasattr(chunk, '__dict__'):
            print(f"  Keys: {list(chunk.__dict__.keys())}")
            # Special handling for tool result visibility
            if hasattr(chunk, 'item') and hasattr(chunk.item, 'tool_result'):
                print(f"  Tool Result: {chunk.item.tool_result}")

def test_decorator_only_unpatched():
    """
    No mcp_servers, no explicit tools → uses only @tool-registered functions:
      - add_numbers
      - greet
    """
    client = OpenAI()  # No patch!
    print("\n=== Test: Decorator-Only (UNPATCHED) ===")
    input = "What is add_numbers(10, 5)? Then greet 'Alice'."
    
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
    
    response = client.responses.create(
        model="gpt-4o-mini",
        input=input,
        tools=tools,
        stream=True
    )
    print_response("UNPATCHED", response)

def test_decorator_only_patched():
    """
    Same test but with patch enabled
    """
    client = patch_openai_with_mcp(OpenAI())
    print("\n=== Test: Decorator-Only (PATCHED) ===")
    input = "What is add_numbers(10, 5)? Then greet 'Alice'."
    
    response = client.responses.create(
        model="gpt-4o-mini",
        input=input,
        stream=True
    )
    print_response("PATCHED", response)

async def test_decorator_only_async_unpatched():
    """
    Same test but with async client (unpatched)
    """
    client = AsyncOpenAI()  # No patch!
    print("\n=== Test: Decorator-Only (ASYNC UNPATCHED) ===")
    input = "What is add_numbers(10, 5)? Then greet 'Alice'."
    
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
    
    response = await client.responses.create(
        model="gpt-4o-mini",
        input=input,
        tools=tools,
        stream=True
    )
    await print_response_async("ASYNC UNPATCHED", response)

async def test_decorator_only_async_patched():
    """
    Same test but with async client (patched)
    """
    client = patch_openai_with_mcp(AsyncOpenAI())
    print("\n=== Test: Decorator-Only (ASYNC PATCHED) ===")
    input = "What is add_numbers(10, 5)? Then greet 'Alice'."
    
    response = await client.responses.create(
        model="gpt-4o-mini",
        input=input,
        stream=True
    )
    await print_response_async("ASYNC PATCHED", response)

def main_sync():
    test_decorator_only_unpatched()
    test_decorator_only_patched()

async def main_async():
    await test_decorator_only_async_unpatched()
    await test_decorator_only_async_patched()

if __name__ == "__main__":
    print("=== SYNC TESTS ===")
    main_sync()
    print("\n=== ASYNC TESTS ===")
    asyncio.run(main_async())