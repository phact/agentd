# tool_registry.py

import inspect
import json
from typing import Callable, Dict, get_type_hints

# 1) This dict will map each function name → (callable, schema_dict)
FUNCTION_REGISTRY: Dict[str, Callable] = {}
SCHEMA_REGISTRY: Dict[str, dict] = {}

def tool(func: Callable) -> Callable:
    """
    Decorator to mark a function as a callable tool:
      1) Inspects its signature & docstring to create a JSON-Schema dict.
      2) Registers the function object in FUNCTION_REGISTRY.
      3) Registers the JSON-Schema in SCHEMA_REGISTRY for use in final_tools.
    """
    sig = inspect.signature(func)
    type_hints = get_type_hints(func)
    doc = func.__doc__ or ""
    first_line, *rest = doc.strip().split("\n", 1)
    description = first_line.strip()

    properties = {}
    required_fields = []
    for name, param in sig.parameters.items():
        py_type = type_hints.get(name, str)
        if py_type is str:
            json_type = "string"
        elif py_type is int:
            json_type = "integer"
        elif py_type is float:
            json_type = "number"
        elif py_type is bool:
            json_type = "boolean"
        else:
            json_type = "string"
        # Find inline param doc from docstring if present
        param_desc = ""
        if rest:
            for line in rest[0].splitlines():
                line = line.strip()
                if line.startswith(f"{name}:"):
                    param_desc = line.split(":", 1)[1].strip()
                    break

        properties[name] = {"type": json_type, "description": param_desc}
        if param.default is inspect.Parameter.empty:
            required_fields.append(name)

    schema = {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required_fields
            }
        }
    }

    # Register the function object and its schema
    FUNCTION_REGISTRY[func.__name__] = func
    SCHEMA_REGISTRY[func.__name__] = schema

    return func
