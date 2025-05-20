import dataclasses
import inspect
import re
from typing import Any, Callable, Type

from pydantic import BaseModel

def resolve_reference(reference: str, references: dict) -> dict:
    return references[reference.split("/")[-1]]

def _pydantic_encoder(obj: Any) -> Any:
    """Simplified version of pydantic v1's deprecated json encoder."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        # Object is a dataclass instance
        return dataclasses.asdict(obj)

    raise TypeError(
        f"Object of type '{obj.__class__.__name__}' is not JSON serializable"
    )

def _name_to_title(name: str) -> str:
    """Converts a camelCase or snake_case name to title case."""
    # If camelCase -> convert to snake case
    name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    name = re.sub("([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    # Convert to title case
    return name.replace("_", " ").strip().title()

def get_single_reference_item(property: dict, references: dict) -> dict:
    # Ref can either be directly in the properties or the first element of allOf
    reference = property.get("$ref")
    if reference is None:
        reference = property["allOf"][0]["$ref"]
    return resolve_reference(reference, references)

def _function_has_named_arg(func: Callable, parameter: str) -> bool:
    try:
        sig = inspect.signature(func)
        for param in sig.parameters.values():
            if param.name == "input":
                return True
    except Exception:
        return False
    return False


def _has_output_ui_renderer(data_item: BaseModel) -> bool:
    return hasattr(data_item, "render_output_ui")


def _has_input_ui_renderer(input_class: Type[BaseModel]) -> bool:
    return hasattr(input_class, "render_input_ui")


def _is_compatible_audio(mime_type: str) -> bool:
    return mime_type in ["audio/mpeg", "audio/ogg", "audio/wav"]


def _is_compatible_image(mime_type: str) -> bool:
    return mime_type in ["image/png", "image/jpeg"]


def _is_compatible_video(mime_type: str) -> bool:
    return mime_type in ["video/mp4"]

def is_single_object(property: dict, references: dict) -> bool:
    try:
        object_reference = get_single_reference_item(property, references)
        if object_reference["type"] != "object":
            return False
        return "properties" in object_reference
    except Exception:
        return False