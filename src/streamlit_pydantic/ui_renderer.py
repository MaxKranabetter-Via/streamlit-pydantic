import base64
import contextlib
import dataclasses
import datetime
import inspect
import json
import mimetypes
import re
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, TypeVar, Union, get_args, get_origin

import pandas as pd
import streamlit as st
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic import dataclasses as pydantic_dataclasses
from pydantic_extra_types.color import Color
from pydantic.fields import FieldInfo

from streamlit_pydantic import schema_utils

_OVERWRITE_STREAMLIT_KWARGS_PREFIX = "st_kwargs_"


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


class GroupOptionalFieldsStrategy(str, Enum):
    NO = "no"
    EXPANDER = "expander"
    SIDEBAR = "sidebar"


class InputUI:
    """Input UI renderer.

    lazydocs: ignore
    """

    def __init__(
        self,
        key: str,
        model: Union[Type[BaseModel], BaseModel],
        streamlit_container: Any = st,
        group_optional_fields: GroupOptionalFieldsStrategy = "no",  # type: ignore
        lowercase_labels: bool = False,
        ignore_empty_values: bool = False,
        return_model: bool = False,
        _editing_stack_key: Optional[str] = None, 
        _current_instance_data: Optional[Dict] = None,
        _is_sub_form: bool = False,
        render_nested_buttons: bool = False
    ):
        self._key = key
        self._return_model = return_model
        self._streamlit_container = streamlit_container
        self._lowercase_labels = lowercase_labels
        self._group_optional_fields = group_optional_fields
        self._ignore_empty_values = ignore_empty_values
        self._is_sub_form = _is_sub_form
        self._render_nested_buttons = render_nested_buttons

        self._session_state = st.session_state

        # Editing stack management (shared across related form instances)
        self._editing_stack_session_key = _editing_stack_key or f"{self._key}-pydantic-editing-stack"
        if self._editing_stack_session_key not in self._session_state:
            self._session_state[self._editing_stack_session_key] = []

        # Data management for the current form level
        self._session_input_key = self._key + "-data" # Unique data store for this form/sub-form level

        initial_data_to_use = _current_instance_data

        if isinstance(model, BaseModel):
            self._input_class = model.__class__
            self._type_adapter = TypeAdapter(self._input_class) if not dataclasses.is_dataclass(self._input_class) else TypeAdapter(pydantic_dataclasses.dataclass(self._input_class))
            self._input_schema = self._type_adapter.json_schema(by_alias=True)
            if initial_data_to_use is None: # Only use model's data if no explicit override
                initial_data_to_use = model.model_dump()
        elif dataclasses.is_dataclass(model) and not isinstance(model, type): # Dataclass instance
            self._input_class = model.__class__ # Get class from instance
            self._type_adapter = TypeAdapter(pydantic_dataclasses.dataclass(self._input_class))
            self._input_schema = self._type_adapter.json_schema(by_alias=True)
            if initial_data_to_use is None:
                 initial_data_to_use = dataclasses.asdict(model)
        elif dataclasses.is_dataclass(model) and isinstance(model, type): # Dataclass type
            self._input_class = model
            self._type_adapter = TypeAdapter(pydantic_dataclasses.dataclass(self._input_class))
            self._input_schema = self._type_adapter.json_schema(by_alias=True)
            if initial_data_to_use is None:
                # For a new form from Type, initialize with defaults
                # Pydantic models handle defaults upon instantiation.
                # For dataclasses, default_factory is used.
                # We will initialize session state with an empty dict, and Pydantic validation will apply defaults.
                initial_data_to_use = {} # Let Pydantic handle defaults on validation / model creation from this
        elif inspect.isclass(model) and issubclass(model, BaseModel): # Pydantic Model Type
            self._input_class = model
            self._type_adapter = TypeAdapter(self._input_class)
            self._input_schema = self._input_class.model_json_schema(by_alias=True)
            if initial_data_to_use is None:
                initial_data_to_use = {} # Let Pydantic handle defaults
        else:
            raise ValueError(f"Unsupported model type: {type(model)}. Must be BaseModel or dataclass (type or instance).")

        if self._session_input_key not in self._session_state or initial_data_to_use is not None:
             # If explicit data is given, or no session data exists, set it.
             # This ensures sub-forms start with the correct slice of data.
             self._session_state[self._session_input_key] = initial_data_to_use if initial_data_to_use is not None else {}


        self._schema_properties = self._input_schema.get("properties", {})
        self._schema_references = self._input_schema.get("$defs", {})
        self._schema_required = self._input_schema.get("required", [])


    def _get_editing_stack(self) -> List[Dict[str, Any]]:
        return self._session_state.get(self._editing_stack_session_key, [])

    def _push_editing_context(self, property_attribute_name: str, nested_model_class: Type[BaseModel], is_new: bool):
        stack = self._get_editing_stack()
        # property_schema is not strictly needed if we have the class
        stack.append({
            "parent_form_key": self._key, 
            "parent_session_data_key": self._session_input_key, 
            "property_attribute_name": property_attribute_name, 
            "nested_model_class": nested_model_class, 
            "is_new": is_new,
        })
        # Force rerun after context push to ensure pydantic_form re-evaluates
        st.rerun()


    def render_ui(self) -> Dict: # Always returns dict of current values for this level
        # This method now just renders its own level. Orchestration is in pydantic_form.
        # Initialize Sessions State for run_id (if not already done globally, though it was in original init)
        if "run_id" not in st.session_state: # Should be handled by top-level form init
            self._session_state.run_id = 0
        
        # Ensure current form's data store is initialized if somehow missed
        if self._session_input_key not in self._session_state :
            self._session_state[self._session_input_key] = self._input_class().model_dump() if issubclass(self._input_class, BaseModel) else dataclasses.asdict(self._input_class())


        properties_in_expander = []

        # The instance_dict for pre-filling values comes from self._session_state[self._session_input_key]
        # This is populated by __init__ or subsequent edits.
        instance_dict = self._session_state.get(self._session_input_key, {})
        # by_alias version is not directly available here, rely on schema keys (which are by_alias if schema was generated that way)

        for property_key in self._schema_properties.keys():
            streamlit_app = self._streamlit_container
            is_optional_field = property_key not in self._schema_required

            if is_optional_field:
                if self._group_optional_fields == "sidebar":
                    streamlit_app = self._streamlit_container.sidebar
                elif self._group_optional_fields == "expander":
                    properties_in_expander.append(property_key)
                    continue

            property_schema = self._schema_properties[property_key]

            if not property_schema.get("title"):
                property_schema["title"] = _name_to_title(property_key)

            # Add current value from session state to property_schema for rendering
            # The _render_property methods expect "init_value" if available
            current_value_for_prop = self._get_value(property_key) # Gets from self._session_state[self._session_input_key]
            if current_value_for_prop is not None:
                 property_schema["init_value"] = current_value_for_prop
            
            # Pass parent_model_class to _render_property for type introspection
            value = self._render_property(streamlit_app, property_key, property_schema, parent_model_class=self._input_class)
            
            if not (value is None and self._is_value_ignored(property_key, value)):
                 # _render_property for nested models with buttons might return current value without re-storing if no change.
                 # If it returns a special marker or None when buttons handle it, this logic might need adjustment.
                 # For now, assume _render_property returns the value to be stored or None.
                 # The "Remove" button directly calls self._store_value(..., None).
                 # Create/Edit buttons call _push_editing_context and rerun, so value storage here is for non-nested or display.
                if not (schema_utils.is_single_object(property_schema, self._schema_references) and \
                        any(btn_key.startswith(f"{self._key}-{property_key}-create") or btn_key.startswith(f"{self._key}-{property_key}-edit") for btn_key in st.session_state.keys() if isinstance(btn_key, str))):
                    # Only store if not a nested object for which a button was just pressed (to avoid overwriting during navigation)
                    # This condition is a bit fragile. A cleaner way is needed.
                    # For now, let's assume _render_property for nested object returns current data and doesn't rely on this to store it if buttons were involved.
                    self._store_value(property_key, value)


        if properties_in_expander:
            with self._streamlit_container.expander("Optional Parameters", expanded=False):
                for property_key in properties_in_expander:
                    property_schema = self._schema_properties[property_key]
                    if not property_schema.get("title"):
                        property_schema["title"] = _name_to_title(property_key)
                    
                    current_value_for_prop = self._get_value(property_key)
                    if current_value_for_prop is not None:
                        property_schema["init_value"] = current_value_for_prop

                    value = self._render_property(self._streamlit_container, property_key, property_schema, parent_model_class=self._input_class)
                    if not (value is None and self._is_value_ignored(property_key, value)):
                            self._store_value(property_key, value)

        # This InputUI always returns the current dict state of its own level
        return self._session_state[self._session_input_key]

    def _get_overwrite_streamlit_kwargs(self, key: str, property: Dict) -> Dict:
        streamlit_kwargs: Dict = {}

        for kwarg in property:
            if kwarg.startswith(_OVERWRITE_STREAMLIT_KWARGS_PREFIX):
                streamlit_kwargs[
                    kwarg.replace(_OVERWRITE_STREAMLIT_KWARGS_PREFIX, "")
                ] = property[kwarg]
        return streamlit_kwargs

    def _get_default_streamlit_input_kwargs(self, key: str, property: Dict) -> Dict:
        label = property.get("title")
        if label and self._lowercase_labels:
            label = label.lower()

        disabled = False
        if property.get("readOnly"):
            # Read only property -> only show value
            disabled = True

        streamlit_kwargs = {
            "label": label,
            "key": str(self._session_state.run_id) + "-" + str(self._key) + "-" + key,
            "disabled": disabled,
            # "on_change": detect_change, -> not supported for inside forms
            # "args": (key,),
        }

        if property.get("description"):
            streamlit_kwargs["help"] = property.get("description")
        elif property.get("help"):
            # Fallback to help. Used more frequently with dataclasses
            streamlit_kwargs["help"] = property.get("help")

        return streamlit_kwargs

    def _is_value_ignored(self, property_key: str, value: Any) -> bool:
        """Returns `True` if the value should be ignored for storing in session.

        This is the case if `ignore_empty_values` is activated and the value is empty and not already set/changed before.
        """
        return (
            self._ignore_empty_values
            and (
                type(value) == int or type(value) == float or isinstance(value, str)
            )  # only for int, float or str
            and not value
            and self._get_value(property_key) is None
        )

    def _store_value_in_state(self, state: dict, key: str, value: Any) -> None:
        key_elements = key.split(".")
        for i, key_element in enumerate(key_elements):
            if i == len(key_elements) - 1:
                # add value to this element
                state[key_element] = value
                return
            if key_element not in state:
                state[key_element] = {}
            state = state[key_element]

    def _get_value_from_state(self, state: dict, key: str) -> Any:
        key_elements = key.split(".")
        for i, key_element in enumerate(key_elements):
            if i == len(key_elements) - 1:
                # add value to this element
                if key_element not in state:
                    return None
                return state[key_element]
            if key_element not in state:
                state[key_element] = {}
            state = state[key_element]
        return None

    def _store_value(self, key: str, value: Any) -> None:
        return self._store_value_in_state(
            self._session_state[self._session_input_key], key, value
        )

    def _get_value(self, key: str) -> Any:
        return self._get_value_from_state(
            self._session_state[self._session_input_key], key
        )

    def _render_single_datetime_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        if property.get("format") == "time":
            if property.get("init_value"):
                streamlit_kwargs["value"] = property.get("init_value")
            elif property.get("default"):
                with contextlib.suppress(Exception):
                    streamlit_kwargs["value"] = datetime.time.fromisoformat(  # type: ignore
                        property["default"]
                    )
            return streamlit_app.time_input(**{**streamlit_kwargs, **overwrite_kwargs})
        elif property.get("format") == "date":
            if property.get("init_value"):
                streamlit_kwargs["value"] = property.get("init_value")
            elif property.get("default"):
                with contextlib.suppress(Exception):
                    streamlit_kwargs["value"] = datetime.date.fromisoformat(  # type: ignore
                        property["default"]
                    )
            return streamlit_app.date_input(**{**streamlit_kwargs, **overwrite_kwargs})
        elif property.get("format") == "date-time":
            if property.get("init_value"):
                streamlit_kwargs["value"] = property.get("init_value")
            elif property.get("default"):
                with contextlib.suppress(Exception):
                    streamlit_kwargs["value"] = datetime.datetime.fromisoformat(  # type: ignore
                        property["default"]
                    )
            with self._streamlit_container.container():
                if not property.get("is_item"):
                    self._streamlit_container.subheader(streamlit_kwargs.get("label"))
                if streamlit_kwargs.get("description"):
                    self._streamlit_container.text(streamlit_kwargs.get("description"))
                selected_date = None
                selected_time = None

                # columns can not be used within a collection
                if property.get("is_item"):
                    date_col = self._streamlit_container.container()
                    time_col = self._streamlit_container.container()
                else:
                    date_col, time_col = self._streamlit_container.columns(2)
                with date_col:
                    date_kwargs = {**{**streamlit_kwargs, **overwrite_kwargs}}
                    date_kwargs["label"] = "Date"
                    date_kwargs["key"] = (f"{streamlit_kwargs.get('key')}-date-input",)

                    value = streamlit_kwargs.get("value")
                    if value:
                        with contextlib.suppress(Exception):
                            date_kwargs["value"] = value.date()
                    selected_date = self._streamlit_container.date_input(**date_kwargs)

                with time_col:
                    time_kwargs = {**{**streamlit_kwargs, **overwrite_kwargs}}
                    time_kwargs["label"] = "Time"
                    time_kwargs["key"] = f"{streamlit_kwargs.get('key')}-time-input"

                    value = streamlit_kwargs.get("value")
                    if value:
                        with contextlib.suppress(Exception):
                            time_kwargs["value"] = value.time()
                    selected_time = self._streamlit_container.time_input(**time_kwargs)

                return datetime.datetime.combine(selected_date, selected_time)
        else:
            streamlit_app.warning(
                "Date format is not supported: " + str(property.get("format"))
            )

    def _render_single_file_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        file_extension = None
        if "mime_type" in property:
            file_extension = mimetypes.guess_extension(property["mime_type"])

        uploaded_file = streamlit_app.file_uploader(
            **{
                **streamlit_kwargs,
                "accept_multiple_files": False,
                "type": file_extension,
                **overwrite_kwargs,
            }
        )
        if uploaded_file is None:
            return b""

        file_bytes = uploaded_file.getvalue()
        if getattr(uploaded_file, "type"):
            if _is_compatible_audio(uploaded_file.type):
                # Show audio
                streamlit_app.audio(file_bytes, format=uploaded_file.type)
            if _is_compatible_image(uploaded_file.type):
                # Show image
                streamlit_app.image(file_bytes)
            if _is_compatible_video(uploaded_file.type):
                # Show video
                streamlit_app.video(file_bytes, format=uploaded_file.type)
        return base64.urlsafe_b64encode(file_bytes)

    def _render_single_string_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)
        if property.get("init_value"):
            streamlit_kwargs["value"] = property.get("init_value")
        elif property.get("default"):
            streamlit_kwargs["value"] = property.get("default")
        elif property.get("example"):
            # TODO: also use example for other property types
            # Use example as value if it is provided
            streamlit_kwargs["value"] = property.get("example")

        if property.get("maxLength") is not None:
            streamlit_kwargs["max_chars"] = property.get("maxLength")

        if property.get("readOnly"):
            # Read only property -> only show value
            streamlit_kwargs["disabled"] = property.get("readOnly", False)

        if property.get("format") == "multi-line" and not property.get("writeOnly"):
            # Use text area if format is multi-line (custom definition)
            return streamlit_app.text_area(**{**streamlit_kwargs, **overwrite_kwargs})
        else:
            # Use text input for most situations
            if property.get("writeOnly"):
                streamlit_kwargs["type"] = "password"
            return streamlit_app.text_input(**{**streamlit_kwargs, **overwrite_kwargs})

    def _render_single_color_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)
        if property.get("init_value") is not None:
            streamlit_kwargs["value"] = property["init_value"]
        elif property.get("default") is not None:
            streamlit_kwargs["value"] = property["default"]
        elif property.get("example") is not None:
            streamlit_kwargs["value"] = property["example"]

        if isinstance(streamlit_kwargs.get("value"), Color):
            streamlit_kwargs["value"] = streamlit_kwargs["value"].as_hex()
        elif isinstance(streamlit_kwargs.get("value"), str):
            streamlit_kwargs["value"] = Color(streamlit_kwargs["value"]).as_hex()

        if property.get("format") == "text":
            # Use text input if specified format is text
            return streamlit_app.text_input(**{**streamlit_kwargs, **overwrite_kwargs})
        else:
            # Use color picker input for most situations
            return streamlit_app.color_picker(
                **{**streamlit_kwargs, **overwrite_kwargs}
            )

    def _render_multi_enum_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        select_options: List[str] = []
        if property.get("items").get("enum"):  # type: ignore
            # Using Literal
            select_options = property.get("items").get("enum")  # type: ignore
        else:
            # Using Enum
            reference_item = schema_utils.resolve_reference(
                property["items"]["$ref"], self._schema_references
            )
            select_options = reference_item["enum"]

        if property.get("init_value"):
            streamlit_kwargs["default"] = property.get("init_value")
        elif property.get("default"):
            try:
                streamlit_kwargs["default"] = property.get("default")
            except Exception:
                pass

        return streamlit_app.multiselect(
            **{**streamlit_kwargs, "options": select_options, **overwrite_kwargs}
        )

    def _render_single_enum_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        select_options: List[str] = []
        if property.get("enum"):
            select_options = property.get("enum")  # type: ignore
        else:
            reference_item = schema_utils.get_single_reference_item(
                property, self._schema_references
            )
            select_options = reference_item["enum"]

        init_value_from_model = property.get("init_value")  # This is from the actual model data
        default_value_from_schema = property.get("default") # This is the default value from the JSON schema

        if init_value_from_model is not None:
            value_to_match = init_value_from_model
            # Check if init_value_from_model is an Enum instance or an object with a .value attribute
            # Ensure it's not a primitive that happens to have a .value attribute (e.g. some custom string/number wrappers)
            if hasattr(init_value_from_model, 'value') and not isinstance(init_value_from_model, (str, int, float, bool)):
                value_to_match = init_value_from_model.value
            
            if value_to_match in select_options:
                streamlit_kwargs["index"] = select_options.index(value_to_match)
            else:
                # If the init_value (or its .value) is not in the options,
                # st.selectbox will default to the first item.
                # This can happen if data is stale or enum definition changed.
                pass 
        elif default_value_from_schema is not None:
            # default_value_from_schema is usually the string/primitive value from the JSON schema
            if default_value_from_schema in select_options:
                streamlit_kwargs["index"] = select_options.index(default_value_from_schema)
            # else: default from schema not in options, let selectbox default to first item.

        # if there is only one option then there is no choice for the user to be make
        # so simply return the value (This is relevant for discriminator properties)
        if len(select_options) == 1:
            return select_options[0]
        else:
            return streamlit_app.selectbox(
                **{**streamlit_kwargs, "options": select_options, **overwrite_kwargs}
            )

    def _render_single_dict_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        # Add title and subheader
        streamlit_app.subheader(property.get("title"))
        if property.get("description"):
            streamlit_app.markdown(property.get("description"))

        if self._get_value(key) is not None or self._get_value(key) == {}:
            data_dict = self._get_value(key)
        elif property.get("init_value"):
            data_dict = property.get("init_value")
        elif property.get("default"):
            data_dict = property.get("default")
        else:
            data_dict = {}

        is_object = True if property["additionalProperties"].get("$ref") else False

        add_col, clear_col, _ = streamlit_app.columns(3)

        add_col = add_col.empty()

        if self._clear_button_allowed(property):
            data_dict = self._render_dict_add_button(key, add_col, data_dict)

        if self._clear_button_allowed(property):
            data_dict = self._render_dict_clear_button(key, clear_col, data_dict)

        new_dict = {}

        for index, input_item in enumerate(data_dict.items()):
            updated_key, updated_value = self._render_dict_item(
                streamlit_app,
                key,
                input_item,
                index,
                property,
            )

            if updated_key is not None and updated_value is not None:
                new_dict[updated_key] = updated_value

            if is_object:
                streamlit_app.markdown("---")

        if not is_object:
            streamlit_app.markdown("---")

        return new_dict

    def _render_single_reference(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        reference_item = schema_utils.get_single_reference_item(
            property, self._schema_references
        )
        return self._render_property(streamlit_app, key, reference_item)

    def _render_union_property(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)

        reference_items = schema_utils.get_union_references(
            property, self._schema_references
        )

        # special handling when there are instance values and a discriminator property
        # to differentiate between object types
        if property.get("init_value") and property.get("discriminator"):
            disc_prop = property["discriminator"]["propertyName"]
            # find the index where the discriminator is equal to the init_value
            ref_index = next(
                i
                for i, x in enumerate(reference_items)
                if x["properties"][disc_prop]["enum"]
                == [property["init_value"][disc_prop]]
            )

            # add any init_value properties to the corresponding reference item
            reference_items[ref_index]["init_value"] = property["init_value"]
            streamlit_kwargs["index"] = ref_index
        elif property.get("init_value") and property.get("instance_class"):
            ref_index = next(
                i
                for i, x in enumerate(reference_items)
                if x["title"] in property["instance_class"]
            )
            reference_items[ref_index]["init_value"] = property["init_value"]
            streamlit_kwargs["index"] = ref_index

        name_reference_mapping: Dict[str, Dict] = {}

        for reference in reference_items:
            reference_title = _name_to_title(reference["title"])
            name_reference_mapping[reference_title] = reference

        streamlit_app.subheader(streamlit_kwargs["label"])  # type: ignore
        if "help" in streamlit_kwargs:
            streamlit_app.markdown(streamlit_kwargs["help"])

        selected_reference = streamlit_app.selectbox(
            **{
                **streamlit_kwargs,
                "label": streamlit_kwargs["label"] + " - Options",
                "options": name_reference_mapping.keys(),
            }
        )

        input_data = self._render_object_input(
            streamlit_app, key, name_reference_mapping[selected_reference]
        )

        streamlit_app.markdown("---")
        return input_data

    def _render_multi_file_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        file_extension = None
        if "mime_type" in property:
            file_extension = mimetypes.guess_extension(property["mime_type"])

        uploaded_files = streamlit_app.file_uploader(
            **{
                **streamlit_kwargs,
                "accept_multiple_files": True,
                "type": file_extension,
                **overwrite_kwargs,
            }
        )
        uploaded_files_bytes = []
        if uploaded_files:
            for uploaded_file in uploaded_files:
                uploaded_files_bytes.append(uploaded_file.read())
        return uploaded_files_bytes

    def _render_single_boolean_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        if "init_value" in property:
            streamlit_kwargs["value"] = property.get("init_value")
        elif "default" in property:
            streamlit_kwargs["value"] = property.get("default")

        # special formatting when rendering within a list/dict
        if property.get("is_item"):
            streamlit_app.markdown("##")

        return streamlit_app.checkbox(**{**streamlit_kwargs, **overwrite_kwargs})

    def _render_single_number_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        number_transform = int
        if property.get("type") == "number":
            number_transform = float  # type: ignore
            streamlit_kwargs["format"] = "%f"

        if "multipleOf" in property:
            # Set stepcount based on multiple of parameter
            streamlit_kwargs["step"] = number_transform(property["multipleOf"])
        elif number_transform == int:
            # Set step size to 1 as default
            streamlit_kwargs["step"] = 1
        elif number_transform == float:
            # Set step size to 0.01 as default
            # TODO: adapt to default value
            streamlit_kwargs["step"] = 0.01

        if "minimum" in property:
            streamlit_kwargs["min_value"] = number_transform(property["minimum"])
        if "exclusiveMinimum" in property:
            streamlit_kwargs["min_value"] = number_transform(
                property["exclusiveMinimum"] + streamlit_kwargs["step"]
            )
        if "maximum" in property:
            streamlit_kwargs["max_value"] = number_transform(property["maximum"])

        if "exclusiveMaximum" in property:
            streamlit_kwargs["max_value"] = number_transform(
                property["exclusiveMaximum"] - streamlit_kwargs["step"]
            )

        if self._session_state.get(streamlit_kwargs["key"]) is None:
            if property.get("init_value") is not None:
                streamlit_kwargs["value"] = number_transform(property["init_value"])
            elif property.get("default") is not None:
                streamlit_kwargs["value"] = number_transform(property["default"])  # type: ignore
            else:
                if "min_value" in streamlit_kwargs:
                    streamlit_kwargs["value"] = streamlit_kwargs["min_value"]
                elif number_transform == int:
                    streamlit_kwargs["value"] = 0
                else:
                    # Set default value to step
                    streamlit_kwargs["value"] = number_transform(
                        streamlit_kwargs["step"]
                    )
        else:
            streamlit_kwargs["value"] = number_transform(
                self._session_state[streamlit_kwargs["key"]]
            )

        if "min_value" in streamlit_kwargs and "max_value" in streamlit_kwargs:
            # TODO: Only if less than X steps
            return streamlit_app.slider(**{**streamlit_kwargs, **overwrite_kwargs})
        else:
            return streamlit_app.number_input(
                **{**streamlit_kwargs, **overwrite_kwargs}
            )

    def _render_object_input(self, streamlit_app: Any, key: str, property: Dict) -> Any:
        properties = property["properties"]
        object_inputs = {}
        for property_key in properties:
            new_property = properties[property_key]
            if not new_property.get("title"):
                # Set property key as fallback title
                new_property["title"] = _name_to_title(property_key)
            # construct full key based on key parts -> required later to get the value
            full_key = key + "." + property_key

            if property.get("init_value"):
                new_property["init_value"] = property["init_value"].get(property_key)
            if property.get("default"):
                new_property["default"] = property["default"].get(property_key)

            new_property["readOnly"] = property.get("readOnly", False)

            value = self._render_property(streamlit_app, full_key, new_property, parent_model_class=self._input_class)
            if not self._is_value_ignored(property_key, value):
                object_inputs[property_key] = value

        return object_inputs

    def _render_single_object_input(
        self, streamlit_app: Any, key: str, property: Dict
    ) -> Any:
        # Add title and subheader
        title = property.get("title")
        if property.get("is_item"):
            streamlit_app.caption(title)
        else:
            streamlit_app.subheader(title)
        if property.get("description"):
            streamlit_app.markdown(property.get("description"))

        object_reference = schema_utils.get_single_reference_item(
            property, self._schema_references
        )

        object_reference["init_value"] = property.get("init_value", None)

        object_reference["default"] = property.get("default", None)

        object_reference["readOnly"] = property.get("readOnly", None)

        return self._render_object_input(streamlit_app, key, object_reference)

    def _render_list_item(
        self,
        streamlit_app: Any,
        parent_key: str,
        value: Any,
        index: int,
        property: Dict[str, Any],
    ) -> Any:
        label = "Item #" + str(index + 1)
        new_key = self._key + "-" + parent_key + "." + str(index)
        item_placeholder = streamlit_app.empty()

        with item_placeholder:
            input_col, button_col = streamlit_app.columns([8, 3])

            button_col.markdown("##")

            if self._remove_button_allowed(index, property):
                remove = False
            else:
                remove = button_col.button("Remove", key=new_key + "-remove")

            #  insert an input field when the remove button has not been clicked
            if not remove:
                with input_col:
                    new_property = {
                        "title": label,
                        "init_value": value if value else None,
                        "is_item": True,
                        "readOnly": property.get("readOnly"),
                        **property["items"],
                    }
                    return self._render_property(streamlit_app, new_key, new_property, parent_model_class=self._input_class)

            else:
                # when the remove button is clicked clear the placeholder and return None
                item_placeholder.empty()
                return None

    def _render_dict_item(
        self,
        streamlit_app: Any,
        parent_key: str,
        in_value: Tuple[str, Any],
        index: int,
        property: Dict[str, Any],
    ) -> Any:
        new_key = self._key + "-" + parent_key + "." + str(index)
        item_placeholder = streamlit_app.empty()

        with item_placeholder.container():
            key_col, value_col, button_col = streamlit_app.columns([4, 4, 3])

            dict_key = in_value[0]
            dict_value = in_value[1]

            dict_key_key = new_key + "-key"
            dict_value_key = new_key + "-value"

            button_col.markdown("##")

            if self._remove_button_allowed(index, property):
                remove = False
            else:
                remove = button_col.button("Remove", key=new_key + "-remove")

            if not remove:
                with key_col:
                    updated_key = streamlit_app.text_input(
                        "Key",
                        value=dict_key,
                        key=dict_key_key,
                        disabled=property.get("readOnly", False),
                    )

                with value_col:
                    new_property = {
                        "title": "Value",
                        "init_value": dict_value,
                        "is_item": True,
                        "readOnly": property.get("readOnly"),
                        **property["additionalProperties"],
                    }
                    with value_col:
                        updated_value = self._render_property(streamlit_app, dict_value_key, new_property, parent_model_class=self._input_class)

                    return updated_key, updated_value

            else:
                # when the remove button is clicked clear the placeholder and return None
                item_placeholder.empty()
                return None, None

    def _add_button_allowed(
        self,
        index: int,
        property: Dict[str, Any],
    ) -> bool:
        add_allowed = not (
            (property.get("readOnly", False) is True)
            or ((index) >= property.get("maxItems", 1000))
        )

        return add_allowed

    def _remove_button_allowed(
        self,
        index: int,
        property: Dict[str, Any],
    ) -> bool:
        remove_allowed = (property.get("readOnly") is True) or (
            (index + 1) <= property.get("minItems", 0)
        )

        return remove_allowed

    def _clear_button_allowed(
        self,
        property: Dict[str, Any],
    ) -> bool:
        clear_allowed = not (
            (property.get("readOnly", False) is True)
            or (property.get("minItems", 0) > 0)
        )

        return clear_allowed

    def _render_list_add_button(
        self,
        key: str,
        streamlit_app: Any,
        data_list: List[Any],
    ) -> List[Any]:
        if streamlit_app.button(
            "Add Item",
            key=self._key + "-" + key + "list-add-item",
        ):
            data_list.append(None)

        return data_list

    def _render_list_clear_button(
        self,
        key: str,
        streamlit_app: Any,
        data_list: List[Any],
    ) -> List[Any]:
        if streamlit_app.button(
            "Clear All",
            key=self._key + "_" + key + "-list_clear-all",
        ):
            data_list = []

        return data_list

    def _render_dict_add_button(
        self, key: str, streamlit_app: Any, data_dict: Dict[str, Any]
    ) -> Dict[str, Any]:
        if streamlit_app.button(
            "Add Item",
            key=self._key + "-" + key + "-add-item",
        ):
            data_dict[str(len(data_dict) + 1)] = None

        return data_dict

    def _render_dict_clear_button(
        self,
        key: str,
        streamlit_app: Any,
        data_dict: Dict[str, Any],
    ) -> Dict[str, Any]:
        if streamlit_app.button(
            "Clear All",
            key=self._key + "-" + key + "-clear-all",
        ):
            data_dict = {}

        return data_dict

    def _render_list_input(self, streamlit_app: Any, key: str, property: Dict) -> Any:
        # Add title and subheader
        streamlit_app.subheader(property.get("title"))
        if property.get("description"):
            streamlit_app.markdown(property.get("description"))

        is_object = True if property["items"].get("$ref") else False

        object_list = []

        # Treat empty list as a session data "hit"
        if self._get_value(key) is not None or self._get_value(key) == []:
            data_list = self._get_value(key)
        elif property.get("init_value"):
            data_list = property.get("init_value")
        elif property.get("default"):
            data_list = property.get("default")
        else:
            data_list = []

        add_col, clear_col, _ = streamlit_app.columns(3)

        add_col = add_col.empty()

        self._render_list_add_button(key, add_col, data_list)

        if self._clear_button_allowed(property):
            data_list = self._render_list_clear_button(key, clear_col, data_list)

        if len(data_list) > 0:
            for index, item in enumerate(data_list):
                output = self._render_list_item(
                    streamlit_app,
                    key,
                    item,
                    index,
                    property,
                )
                if output is not None:
                    object_list.append(output)

                if is_object:
                    streamlit_app.markdown("---")

            if not self._add_button_allowed(len(object_list), property):
                add_col = add_col.empty()

            if not is_object:
                streamlit_app.markdown("---")

        return object_list

    def _render_property(self, streamlit_app: Any, key: str, property: Dict, parent_model_class: Type[BaseModel]) -> Any:
        # filter the case of optional and nullable
        property_schema = schema_utils.filter_nullable(property)

        # Handle nested Pydantic Models (single objects) with buttons
        if schema_utils.is_single_object(property_schema, self._schema_references):
            target_field_info: Optional[FieldInfo] = None
            target_attr_name: Optional[str] = None

            # Find Pydantic FieldInfo: key is from schema (potentially alias)
            # Parent_model_class.model_fields is keyed by attribute name.
            for attr_name_iter, field_info_iter in parent_model_class.model_fields.items():
                if field_info_iter.alias == key:
                    target_field_info = field_info_iter
                    target_attr_name = attr_name_iter
                    break
                # Fallback if alias not used in schema key for some reason (should not happen with by_alias=True)
                if not target_field_info and attr_name_iter == key: # Check if key is already the attribute name
                     target_field_info = field_info_iter
                     target_attr_name = attr_name_iter
            
            if not target_field_info and key in parent_model_class.model_fields: # Final check if key is attr_name
                target_attr_name = key
                target_field_info = parent_model_class.model_fields[key]


            nested_model_class: Optional[Type[BaseModel]] = None
            if target_field_info:
                actual_type = target_field_info.annotation
                origin_type = get_origin(actual_type)
                
                types_to_check = []
                if origin_type is Union or str(origin_type) == "typing.Optional" or str(origin_type) == "Optional": # Optional is Union[X, None]
                    args = get_args(actual_type)
                    types_to_check.extend([t for t in args if t is not type(None)])
                elif actual_type is not type(None):
                    types_to_check.append(actual_type)

                for t in types_to_check:
                    if inspect.isclass(t) and issubclass(t, BaseModel):
                        nested_model_class = t
                        break
            
            current_value = self._get_value(key) # Get current data for the nested model field

            if nested_model_class and target_attr_name:
                # We have a recognized nested BaseModel field.
                # Conditionally render buttons or inline form.
                if self._render_nested_buttons:
                    field_title = property_schema.get("title", key)
                    cols = streamlit_app.columns([3,1,1,1]) if current_value is not None and (key not in self._schema_required) else streamlit_app.columns([3,1,1])
                    with cols[0]:
                        st.markdown(f"**{field_title}**")
                        if current_value is not None:
                            st.json(current_value, expanded=False)
                        elif key not in self._schema_required:
                            st.caption("Optional field, not set.")
                        else:
                            st.caption("Required field, not set.")

                    button_key_base = f"{self._key}-{self._session_state.run_id}-{target_attr_name}"

                    if current_value is None:
                        if cols[1].button("➕ Create", key=f"{button_key_base}-create", help=f"Create {field_title}"):
                            self._push_editing_context(target_attr_name, nested_model_class, is_new=True)
                    else:
                        if cols[1].button("✏️ Edit", key=f"{button_key_base}-edit", help=f"Edit {field_title}"):
                            self._push_editing_context(target_attr_name, nested_model_class, is_new=False)
                    
                    if current_value is not None and (key not in self._schema_required):
                        remove_button_col_idx = 2
                        if cols[remove_button_col_idx].button("➖ Remove", key=f"{button_key_base}-remove", help=f"Remove {field_title}"):
                            self._store_value(key, None)
                            st.rerun()
                    return current_value # Buttons handle navigation
                else:
                    # Fallback for pydantic_form: render inline (original behavior)
                    return self._render_single_object_input(streamlit_app, key, property_schema)

            else: # Not a recognized nested BaseModel or type extraction failed, fallback
                # Fallback to original rendering for objects if type inspection fails
                return self._render_single_object_input(streamlit_app, key, property_schema)


        if schema_utils.is_single_enum_property(property_schema, self._schema_references):
            return self._render_single_enum_input(streamlit_app, key, property_schema)

        if schema_utils.is_multi_enum_property(property_schema, self._schema_references):
            return self._render_multi_enum_input(streamlit_app, key, property_schema)

        if schema_utils.is_single_file_property(property_schema):
            return self._render_single_file_input(streamlit_app, key, property_schema)

        if schema_utils.is_multi_file_property(property_schema):
            return self._render_multi_file_input(streamlit_app, key, property_schema)

        if schema_utils.is_single_datetime_property(property_schema):
            return self._render_single_datetime_input(streamlit_app, key, property_schema)

        if schema_utils.is_single_color_property(property_schema):
            return self._render_single_color_input(streamlit_app, key, property_schema)

        if schema_utils.is_single_boolean_property(property_schema):
            return self._render_single_boolean_input(streamlit_app, key, property_schema)

        if schema_utils.is_single_dict_property(property_schema):
            return self._render_single_dict_input(streamlit_app, key, property_schema)

        if schema_utils.is_single_number_property(property_schema):
            return self._render_single_number_input(streamlit_app, key, property_schema)

        if schema_utils.is_single_string_property(property_schema):
            return self._render_single_string_input(streamlit_app, key, property_schema)

        if schema_utils.is_single_object(property_schema, self._schema_references):
            return self._render_single_object_input(streamlit_app, key, property_schema)

        if schema_utils.is_object_list_property(property_schema, self._schema_references):
            return self._render_list_input(streamlit_app, key, property_schema)

        if schema_utils.is_property_list(property_schema):
            return self._render_list_input(streamlit_app, key, property_schema)

        if schema_utils.is_single_reference(property_schema):
            # This might be another path for nested objects if not caught by is_single_object first.
            # For now, assume is_single_object with type checking is primary.
            return self._render_single_reference(streamlit_app, key, property_schema)

        if schema_utils.is_union_property(property_schema):
            return self._render_union_property(streamlit_app, key, property_schema)

        streamlit_app.warning(
            "The type of the following property is currently not supported: "
            + str(property_schema.get("title"))
        )
        raise Exception("Unsupported property")


class OutputUI:
    """Output UI renderer.

    lazydocs: ignore
    """

    def __init__(self, output_data: Any, input_data: Optional[Any] = None):
        self._output_data = output_data
        self._input_data = input_data

    def render_ui(self) -> None:
        try:
            if isinstance(self._output_data, BaseModel):
                self._render_single_output(st, self._output_data)
                return
            if type(self._output_data) == list:
                self._render_list_output(st, self._output_data)
                return
        except Exception as ex:
            st.exception(ex)
            # TODO: Fallback to
            # st.json(jsonable_encoder(self._output_data))

    def _render_single_text_property(
        self, streamlit: Any, property_schema: Dict, value: Any
    ) -> None:
        # Add title and subheader
        streamlit.subheader(property_schema.get("title"))
        if property_schema.get("description"):
            streamlit.markdown(property_schema.get("description"))
        if value is None or value == "":
            streamlit.info("No value returned!")
        else:
            streamlit.code(str(value), language="plain")

    def _render_single_file_property(
        self, streamlit: Any, property_schema: Dict, value: Any
    ) -> None:
        # Add title and subheader
        streamlit.subheader(property_schema.get("title"))
        if property_schema.get("description"):
            streamlit.markdown(property_schema.get("description"))
        if value is None or len(value) == 0:
            streamlit.info("No value returned!")
        else:
            # TODO: detect if it is base64
            file_extension = ""
            if "mime_type" in property_schema:
                mime_type = property_schema["mime_type"]
                file_extension = mimetypes.guess_extension(mime_type) or ""

                if _is_compatible_audio(mime_type):
                    streamlit.audio(value, format=mime_type)
                    return

                if _is_compatible_image(mime_type):
                    streamlit.image(value)
                    return

                if _is_compatible_video(mime_type):
                    streamlit.video(value, format=mime_type)
                    return

            filename = (
                (property_schema["title"] + file_extension)
                .lower()
                .strip()
                .replace(" ", "-")
            )
            st.download_button("Download File", value, file_name=filename)

    def _render_single_complex_property(
        self, streamlit: Any, property_schema: Dict, value: Any
    ) -> None:
        # Add title and subheader
        streamlit.subheader(property_schema.get("title"))
        if property_schema.get("description"):
            streamlit.markdown(property_schema.get("description"))

        streamlit.json(json.dumps(value, default=_pydantic_encoder))

    def _render_single_output(self, streamlit: Any, output_data: BaseModel) -> None:
        try:
            if _has_output_ui_renderer(output_data):
                if _function_has_named_arg(output_data.render_output_ui, "input"):  # type: ignore
                    # render method also requests the input data
                    output_data.render_output_ui(streamlit, input=self._input_data)  # type: ignore
                else:
                    output_data.render_output_ui(streamlit)  # type: ignore
                return
        except Exception:
            # TODO
            pass
            # Use default auto-generation methods if the custom rendering throws an exception
            # logger.exception(
            #    "Failed to execute custom render_output_ui function. Using auto-generation instead"
            # )

        model_schema = output_data.model_json_schema(by_alias=False)
        model_properties = model_schema.get("properties")
        definitions = model_schema.get("$defs")

        if model_properties:
            for property_key in output_data.__dict__:
                property_schema = model_properties.get(property_key)
                if not property_schema.get("title"):
                    # Set property key as fallback title
                    property_schema["title"] = property_key

                output_property_value = output_data.__dict__[property_key]

                if _has_output_ui_renderer(output_property_value):
                    output_property_value.render_output_ui(streamlit)  # type: ignore
                    continue

                if isinstance(output_property_value, BaseModel):
                    # Render output recursivly
                    streamlit.subheader(property_schema.get("title"))
                    if property_schema.get("description"):
                        streamlit.markdown(property_schema.get("description"))
                    self._render_single_output(streamlit, output_property_value)
                    continue

                if property_schema:
                    property_schema = schema_utils.filter_nullable(property_schema)
                    if schema_utils.is_multi_file_property(property_schema):
                        for file in output_property_value:
                            self._render_single_file_property(
                                streamlit, property_schema, file
                            )
                        continue

                    if schema_utils.is_single_file_property(property_schema):
                        self._render_single_file_property(
                            streamlit, property_schema, output_property_value
                        )
                        continue

                    if (
                        schema_utils.is_single_string_property(property_schema)
                        or schema_utils.is_single_number_property(property_schema)
                        or schema_utils.is_single_datetime_property(property_schema)
                        or schema_utils.is_single_boolean_property(property_schema)
                    ):
                        self._render_single_text_property(
                            streamlit, property_schema, output_property_value
                        )
                        continue
                    if definitions and schema_utils.is_single_enum_property(
                        property_schema, definitions
                    ):
                        self._render_single_text_property(
                            streamlit, property_schema, output_property_value.value
                        )
                        continue

                    if isinstance(output_property_value, (set, dict, tuple)):
                        self._render_single_text_property(
                            streamlit, property_schema, output_property_value
                        )
                        continue

                    # TODO: render dict as table

                    self._render_single_complex_property(
                        streamlit, property_schema, output_property_value
                    )
            return

        # Display single field in code block:
        # if len(output_data.__dict__) == 1:
        #     value = next(iter(output_data.__dict__.values()))

        #     if type(value) in (int, float, str):
        #         # Should not be a complex object (with __dict__) -> should be a primitive
        #         # hasattr(output_data.__dict__[0], '__dict__')
        #         streamlit.subheader("This is a test:")
        #         streamlit.code(value, language="plain")
        #         return

        st.error("Cannot render output")
        # TODO: Fallback to json output
        # streamlit.json(jsonable_encoder(output_data))

    def _render_list_output(self, streamlit: Any, output_data: List) -> None:
        try:
            data_items: List = []
            for data_item in output_data:
                if _has_output_ui_renderer(data_item):
                    # Render using the render function
                    data_item.render_output_ui(streamlit)  # type: ignore
                    continue
                data_items.append(data_item.model_dump())
            # Try to show as dataframe
            streamlit.table(pd.DataFrame(data_items))
        except Exception:
            st.error("Cannot render output list")
            # TODO Fallback to
            # streamlit.json(jsonable_encoder(output_data))


def pydantic_input(
    key: str,
    model: Type[BaseModel],
    group_optional_fields: GroupOptionalFieldsStrategy = "no",  # type: ignore
    lowercase_labels: bool = False,
    ignore_empty_values: bool = False,
) -> Dict:
    """Auto-generates input UI elements for a selected Pydantic class.

    Args:
        key (str): A string that identifies the form. Each form must have its own key.
        model (Type[BaseModel]): The input model. Either a class or instance based on Pydantic `BaseModel` or Python `dataclass`.
        group_optional_fields (str, optional): If `sidebar`, optional input elements will be rendered on the sidebar.
            If `expander`,  optional input elements will be rendered inside an expander element. Defaults to `no`.
        lowercase_labels (bool): If `True`, all input element labels will be lowercased. Defaults to `False`.
        ignore_empty_values (bool): If `True`, empty values for strings and numbers will not be stored in the session state. Defaults to `False`.

    Returns:
        Dict: A dictionary with the current state of the input data.
    """
    return InputUI(
        key,
        model,
        group_optional_fields=group_optional_fields,
        lowercase_labels=lowercase_labels,
        ignore_empty_values=ignore_empty_values,
        return_model=False,
    ).render_ui()


def pydantic_output(output_data: Any) -> None:
    """Auto-generates output UI elements for all properties of a (Pydantic-based) model instance.

    Args:
        output_data (Any): The output data.
    """

    OutputUI(output_data).render_ui()


# Define generic type to allow autocompletion for the model fields
T = TypeVar("T", bound=BaseModel)


def pydantic_form(
    key: str,
    model: Type[T],
    submit_label: str = "Submit",
    clear_on_submit: bool = False,
    group_optional_fields: GroupOptionalFieldsStrategy = "no",
    lowercase_labels: bool = False,
    ignore_empty_values: bool = False,
) -> Optional[T]:
    """Auto-generates a Streamlit form based on the given (Pydantic-based) input class.
       Supports nested model editing via a stacked navigation approach.
    """

    editing_stack_session_key = f"{key}-pydantic-editing-stack"
    if editing_stack_session_key not in st.session_state:
        st.session_state[editing_stack_session_key] = []
    
    editing_stack: List[Dict[str, Any]] = st.session_state[editing_stack_session_key]

    current_model_to_render_class: Type[BaseModel] = model
    current_input_ui_key = key 
    data_for_current_form: Optional[Dict] = None
    
    # Path within the root_data to the current sub-model's data being edited
    path_to_current_model_in_root_session_data: List[str] = [] 
    is_sub_form = bool(editing_stack)

    # Initialize root model's data store in session state if it doesn't exist
    root_model_session_data_key = key + "-data"
    if root_model_session_data_key not in st.session_state:
        # Initialize with an empty dict. InputUI will handle rendering fields
        # and Pydantic defaults from schema will be used for widgets.
        st.session_state[root_model_session_data_key] = {}


    if editing_stack:
        top_context = editing_stack[-1]
        current_model_to_render_class = top_context["nested_model_class"]
        
        # Create a unique key for the InputUI instance of the sub-form
        # Path helps ensure uniqueness if same field name appears at different nesting levels
        temp_path_keys_for_key = []
        for i_ctx, ctx_item_for_key in enumerate(editing_stack):
            temp_path_keys_for_key.append(ctx_item_for_key["property_attribute_name"])
        unique_path_str_for_key = "_".join(temp_path_keys_for_key)
        current_input_ui_key = f"{key}-edit-{unique_path_str_for_key}-{len(editing_stack)}"
        
        # Retrieve the current data for the nested model from the root session data
        current_parent_data_slice = st.session_state[root_model_session_data_key]
        
        for i_ctx, ctx_item in enumerate(editing_stack):
            attr_name = ctx_item["property_attribute_name"]
            path_to_current_model_in_root_session_data.append(attr_name)
            
            is_target_attribute = (i_ctx == len(editing_stack) - 1)

            if not isinstance(current_parent_data_slice, dict):
                st.error(f"Data structure error: Expected a dictionary at path leading to '{attr_name}'. Found: {type(current_parent_data_slice)}")
                valid_path = False
                break

            if attr_name not in current_parent_data_slice or current_parent_data_slice[attr_name] is None:
                # Attribute doesn't exist or is None
                if is_target_attribute: # This is the one we want to edit/create
                    if top_context["is_new"] or current_parent_data_slice.get(attr_name) is None:
                        current_parent_data_slice[attr_name] = {} # Initialize as empty dict for the new/edited form
                        data_for_current_form = current_parent_data_slice[attr_name]
                    else:
                        # This case implies editing an existing non-dict, non-None value as if it were a new model, which is problematic.
                        # Or, is_new is False but data is missing. Should be caught by earlier checks.
                        st.error(f"Error: Trying to edit non-existent or non-dict data for '{attr_name}' without 'is_new' flag or data was None initially.")
                        valid_path = False
                        break
                else: # Intermediate path, create empty dict to continue traversal
                    current_parent_data_slice[attr_name] = {}
                    current_parent_data_slice = current_parent_data_slice[attr_name]
            
            elif not isinstance(current_parent_data_slice[attr_name], dict):
                # Attribute exists but is not a dictionary (e.g., a primitive, list, etc.)
                if is_target_attribute: # This is the one we want to edit
                    # This is an issue: trying to edit a non-dict as a nested model.
                    # However, is_single_object check in InputUI should prevent pushing such context.
                    # If we are here, it implies a logic flaw or schema mismatch.
                    st.error(f"Data structure error: '{attr_name}' is not a dictionary but is being edited as a nested model. Value: {current_parent_data_slice[attr_name]}")
                    valid_path = False
                    break
                else: # Intermediate path has non-dict value where dict was expected
                    st.error(f"Data structure error: Intermediate path '{attr_name}' is not a dictionary. Cannot navigate further.")
                    valid_path = False
                    break
            else:
                # Attribute exists and is a dictionary
                if is_target_attribute:
                    data_for_current_form = current_parent_data_slice[attr_name]
                else:
                    current_parent_data_slice = current_parent_data_slice[attr_name]
        
        if not valid_path:
            # This handles errors like non-dict in path, or trying to edit non-existent without is_new
            if not top_context["is_new"] or (data_for_current_form is None and not top_context["is_new"]):
                 st.error(f"Could not load or initialize data for the nested form: {'.'.join(path_to_current_model_in_root_session_data)}. Returning to previous level.")
                 editing_stack.pop()
                 st.rerun()
                 return st.session_state[root_model_session_data_key]

    # Use a consistent key for the streamlit `st.form` itself, possibly based on current_input_ui_key
    # This ensures that clear_on_submit applies to the correct form view.
    streamlit_form_key = f"{current_input_ui_key}-streamlit-form-wrapper"

    with st.form(key=streamlit_form_key, clear_on_submit=clear_on_submit if not is_sub_form else False): # clear_on_submit for root form only
        ui_instance = InputUI(
            key=current_input_ui_key, 
            model=current_model_to_render_class, 
            group_optional_fields=group_optional_fields,
            lowercase_labels=lowercase_labels,
            ignore_empty_values=ignore_empty_values,
            return_model=False, # InputUI.render_ui will return dict, pydantic_form handles model validation
            _editing_stack_key=editing_stack_session_key, 
            _current_instance_data=data_for_current_form,
            _is_sub_form=is_sub_form,
            render_nested_buttons=False # pydantic_form should not render nested buttons
        )
        
        # This call renders the UI fields and updates st.session_state[current_input_ui_key + "-data"]
        ui_instance.render_ui() 
        
        # Submit button for the current form (root or nested)
        # For sub-forms, this acts as "Save and Close"
        form_submit_label = "Save and Close" if is_sub_form else submit_label
        submitted = st.form_submit_button(label=form_submit_label)

        if submitted:
            current_form_output_data_dict = st.session_state[current_input_ui_key + "-data"]
            try:
                # Validate the data from the current form against its model class
                validated_instance = current_model_to_render_class.model_validate(current_form_output_data_dict)
                
                if is_sub_form:
                    # Save nested form's data back to the parent in the root session state
                    data_to_update_in_root = st.session_state[root_model_session_data_key]
                    ptr = data_to_update_in_root
                    for i, part_key in enumerate(path_to_current_model_in_root_session_data):
                        if i == len(path_to_current_model_in_root_session_data) - 1:
                            ptr[part_key] = validated_instance.model_dump()
                        else:
                            if not isinstance(ptr.get(part_key), dict): 
                                ptr[part_key] = {} # Ensure path exists
                            ptr = ptr[part_key]
                    
                    # Clean up the session state for the sub-form that was just submitted
                    if (current_input_ui_key + "-data") in st.session_state:
                        del st.session_state[current_input_ui_key + "-data"]

                    editing_stack.pop() 
                    st.rerun() 
                    return None # Indicates submission processed, page will reload
                else:
                    # This was the root form submission
                    if clear_on_submit: # Clear root form's data from session
                         st.session_state[root_model_session_data_key] = model().model_dump()

                    return validated_instance 
            
            except ValidationError as ex:
                # InputUI render_ui itself might show warnings if its return_model was true.
                # Here, we ensure errors for the final submission are also shown.
                error_text = "**Input failed validation:**"
                for error in ex.errors():
                    location = ".".join(str(loc) for loc in error["loc"]) if "loc" in error else "Field"
                    error_text += f"\\n\\n**{location}:** {error['msg']}"
                st.warning(error_text) # Use warning to be consistent with InputUI
                return None 
        
        # "Cancel" button for nested forms (rendered outside st.form_submit_button logic)
        if is_sub_form:
            # Place it beside the submit button if possible, or just below.
            # st.form_submit_button is special, other buttons might not align well within st.form if not careful.
            # For simplicity, render it directly. It won't submit the form, just navigate.
            # This button should ideally be outside the `with st.form` or handled carefully.
            # Let's put a separate cancel button outside the submit logic but within the form scope for layout.
            # However, a button inside st.form usually triggers form submission.
            # A common pattern is to use st.columns for side-by-side buttons for submit/cancel.
            pass # Let's see how submit button works first. A separate cancel button outside form might be cleaner.

    # Cancel button for sub-forms (placed outside the main form block for independent action)
    if is_sub_form:
        if st.button("❌ Cancel and Discard Changes", key=f"{current_input_ui_key}-external-cancel"):
            # Clean up the session state for the sub-form
            if (current_input_ui_key + "-data") in st.session_state:
                del st.session_state[current_input_ui_key + "-data"]
            editing_stack.pop()
            st.rerun()
            return None # To satisfy Optional[T] return

    return None # No submission or navigated away


def pydantic_nested_input(
    key: str,
    model: Type[T],
    group_optional_fields: GroupOptionalFieldsStrategy = "no",
    lowercase_labels: bool = False,
    ignore_empty_values: bool = False,
) -> Dict[str, Any]:
    """Auto-generates a Streamlit UI for a Pydantic model, supporting deeply nested editing.

    This function does NOT use a top-level st.form, allowing for custom button handling
    for navigation within nested models.

    Args:
        key (str): A unique key for this input group. Used for session state management.
        model (Type[BaseModel]): The root Pydantic model class.
        group_optional_fields (str, optional): Strategy for grouping optional fields.
        lowercase_labels (bool): If True, all labels are lowercased.
        ignore_empty_values (bool): If True, empty strings/numbers are not stored.

    Returns:
        Dict[str, Any]: The current state of the root model's data as a dictionary.
    """
    editing_stack_session_key = f"{key}-pydantic-editing-stack"
    if editing_stack_session_key not in st.session_state:
        st.session_state[editing_stack_session_key] = []
    
    editing_stack: List[Dict[str, Any]] = st.session_state[editing_stack_session_key]

    current_model_to_render_class: Type[BaseModel] = model
    # current_input_ui_key is the key for InputUI instance for the *current* view (root or nested)
    current_input_ui_key = key 
    # data_for_current_form is the specific dict slice for the *current* view
    data_for_current_form: Optional[Dict] = None 
    
    path_to_current_model_in_root_session_data: List[str] = [] 
    is_sub_view = bool(editing_stack)

    # root_model_session_data_key is the key for the *entire* data of the root model instance
    root_model_session_data_key = key + "-root-data"
    if root_model_session_data_key not in st.session_state:
        st.session_state[root_model_session_data_key] = {}

    if is_sub_view:
        top_context = editing_stack[-1]
        current_model_to_render_class = top_context["nested_model_class"]
        
        temp_path_keys_for_key = [ctx["property_attribute_name"] for ctx in editing_stack]
        unique_path_str_for_key = "_".join(temp_path_keys_for_key)
        current_input_ui_key = f"{key}-nested-edit-{unique_path_str_for_key}-{len(editing_stack)}"
        
        current_parent_data_slice = st.session_state[root_model_session_data_key]
        valid_path = True
        # Clear path_to_current_model_in_root_session_data before rebuilding
        path_to_current_model_in_root_session_data.clear() 

        for i_ctx, ctx_item in enumerate(editing_stack):
            attr_name = ctx_item["property_attribute_name"]
            path_to_current_model_in_root_session_data.append(attr_name)

            is_target_attribute = (i_ctx == len(editing_stack) - 1)

            if not isinstance(current_parent_data_slice, dict):
                st.error(f"Data structure error: Expected a dictionary at path leading to '{attr_name}'. Found: {type(current_parent_data_slice)}")
                valid_path = False
                break

            if attr_name not in current_parent_data_slice or current_parent_data_slice[attr_name] is None:
                if is_target_attribute:
                    if top_context["is_new"] or current_parent_data_slice.get(attr_name) is None:
                        current_parent_data_slice[attr_name] = {} 
                        data_for_current_form = current_parent_data_slice[attr_name]
                    else:
                        st.error(f"Error: Trying to edit non-existent or non-dict data for '{attr_name}' without 'is_new' flag or data was None initially.")
                        valid_path = False
                        break
                else: 
                    current_parent_data_slice[attr_name] = {}
                    current_parent_data_slice = current_parent_data_slice[attr_name]
            elif not isinstance(current_parent_data_slice[attr_name], dict):
                if is_target_attribute:
                    st.error(f"Data structure error: '{attr_name}' is not a dictionary but is being edited as a nested model. Value: {current_parent_data_slice[attr_name]}")
                    valid_path = False
                    break
                else: 
                    st.error(f"Data structure error: Intermediate path '{attr_name}' is not a dictionary. Cannot navigate further.")
                    valid_path = False
                    break
            else:
                if is_target_attribute:
                    data_for_current_form = current_parent_data_slice[attr_name]
                else:
                    current_parent_data_slice = current_parent_data_slice[attr_name]
        
        if not valid_path:
            # This error handling block is for issues during path traversal for a sub_view
            st.error(f"Could not load or initialize data for the nested form: {'.'.join(path_to_current_model_in_root_session_data)}. Returning to previous level.")
            editing_stack.pop()
            st.rerun()
            return st.session_state[root_model_session_data_key] # Should not be reached
        
        # If valid_path is True, we can proceed to display the sub_view UI
        if top_context["is_new"] and data_for_current_form is None:
             # This case might be redundant if data_for_current_form is already {} from above logic
            data_for_current_form = {} 
        
        # Display breadcrumbs or navigation path FOR SUB-VIEWS
        nav_path_display = "Editing: " + model.__name__ + " -> " + " -> ".join(path_to_current_model_in_root_session_data)
        st.caption(nav_path_display)

    else:
        # This is the ROOT VIEW logic
        data_for_current_form = st.session_state[root_model_session_data_key]
        current_input_ui_key = key + "-root-ui" 
        # No navigation path caption for the root view

    # Common UI rendering logic starts here
    ui_instance = InputUI(
        key=current_input_ui_key, 
        model=current_model_to_render_class, 
            group_optional_fields=group_optional_fields,
            lowercase_labels=lowercase_labels,
            ignore_empty_values=ignore_empty_values,
        return_model=False, 
        _editing_stack_key=editing_stack_session_key,
        _current_instance_data=data_for_current_form,
        _is_sub_form=is_sub_view, # To inform InputUI it's part of a nested flow if needed
        render_nested_buttons=True # Critical: enable Create/Edit/Remove buttons
    )
    
    # This renders the fields. Data is updated in st.session_state[current_input_ui_key + "-data"]
    ui_instance.render_ui()

    if is_sub_view:
        cols = st.columns(2)
        with cols[0]:
            if st.button("💾 Save Changes", key=f"{current_input_ui_key}-save-nested"):
                current_form_output_data_dict = st.session_state.get(current_input_ui_key + "-data", {})
                try:
                    validated_instance = current_model_to_render_class.model_validate(current_form_output_data_dict)
                    
                    data_to_update_in_root = st.session_state[root_model_session_data_key]
                    ptr = data_to_update_in_root
                    for i, part_key in enumerate(path_to_current_model_in_root_session_data):
                        if i == len(path_to_current_model_in_root_session_data) - 1:
                            ptr[part_key] = validated_instance.model_dump()
                        else:
                            if not isinstance(ptr.get(part_key), dict) or ptr[part_key] is None:
                                ptr[part_key] = {} 
                            ptr = ptr[part_key]
                    
                    # Clean up session state for the sub-view specific InputUI instance
                    if (current_input_ui_key + "-data") in st.session_state:
                        del st.session_state[current_input_ui_key + "-data"]

                    editing_stack.pop() 
                    st.rerun() 
                
                except ValidationError as ex:
                    error_text = "**Input failed validation:**"
                    for error in ex.errors():
                        location = ".".join(str(loc) for loc in error["loc"]) if "loc" in error else "Field"
                        error_text += f"\\n\\n**{location}:** {error['msg']}"
                    st.warning(error_text)
        with cols[1]:
            if st.button("❌ Cancel & Discard", key=f"{current_input_ui_key}-cancel-nested"):
                # Clean up session state for the sub-view specific InputUI instance
                if (current_input_ui_key + "-data") in st.session_state:
                    del st.session_state[current_input_ui_key + "-data"]
                editing_stack.pop()
                st.rerun()

    nav_path_display = "Editing: " + model.__name__ + " -> " + " -> ".join(path_to_current_model_in_root_session_data)
    st.caption(nav_path_display) # This displays the path for sub-views

    return st.session_state[root_model_session_data_key]
