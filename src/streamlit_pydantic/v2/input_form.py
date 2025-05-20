import inspect
from typing import Any, Optional, Type, Union, get_args, get_origin
from pydantic import BaseModel, TypeAdapter
from pydantic.fields import FieldInfo
import streamlit as st

from streamlit_pydantic.v2.ui_renderer_v2 import StreamlitRenderer
from streamlit_pydantic.v2.utils import _name_to_title, is_single_object


class InputUI:

    def __init__(self, key: str, model: BaseModel, streamlit_container: Any = st):
        self.key = key
        self.model = model
        self.st = streamlit_container
        self.split_nested_models = True

        self._load_session(model)

    def _load_session(self, model: BaseModel):
        self._session_state = st.session_state
        if "run_id" not in st.session_state:
            self._session_state.run_id = 0
        self._session_input_key = self.key + "-data"
        if self._session_input_key not in st.session_state:
            self._session_state[self._session_input_key] = {}

        self._editing_stack_session_key = f"{self.key}-pydantic-editing-stack" # stack used to track nested editing contexts (we open and close editing contexts when creating and editing nested models)
        if self._editing_stack_session_key not in self._session_state:
            self._session_state[self._editing_stack_session_key] = []

        self._type_adapter = None
        self._input_schema = model.model_json_schema(by_alias=True)
        self._input_class = model

        self._schema_properties = self._input_schema.get("properties", {})
        self._schema_references = self._input_schema.get("$defs", {})
        self._schema_required = self._input_schema.get("required", [])

        self.renderer = StreamlitRenderer(run_id=self._session_state.run_id, key=self.key, schema_references=self._schema_references)

    def render_ui(self):
        for property_key in self._schema_properties.keys():
            self.render_property(property_key)

    def render_property(self, property_key: str):
        # data_dict = self._get_value(property_key) is not None or self._get_value(property_key) == {}
        # data_list = self._get_value(property_key) is not None or self._get_value(property_key) == []
        property_schema = self._schema_properties[property_key]

        if not property_schema.get("title"):
            # Set property key as fallback title
            property_schema["title"] = _name_to_title(property_key)

        if is_single_object(property_schema, self._schema_references):
            value = self.render_pydantic_property(property_key, property_schema, self._input_class)
        else:
            value = self.renderer.render_property(self.st, property_key, property_schema, data=self._get_value(property_key))

    def render_pydantic_property(self,
                                 property_key: str,
                                 property_schema: dict[str, Any],
                                 parent_model_class: Type[BaseModel]):
        target_field_info: Optional[FieldInfo] = None
        target_attr_name: Optional[str] = None

        # Find Pydantic FieldInfo: key is from schema (potentially alias)
        # Parent_model_class.model_fields is keyed by attribute name.
        for attr_name_iter, field_info_iter in parent_model_class.model_fields.items():
            if field_info_iter.alias == property_key:
                target_field_info = field_info_iter
                target_attr_name = attr_name_iter
                break
            # Fallback if alias not used in schema key for some reason (should not happen with by_alias=True)
            if not target_field_info and attr_name_iter == property_key: # Check if key is already the attribute name
                    target_field_info = field_info_iter
                    target_attr_name = attr_name_iter

        if not target_field_info and property_key in parent_model_class.model_fields: # Final check if key is attr_name
            target_attr_name = property_key
            target_field_info = parent_model_class.model_fields[property_key]


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

        current_value = self._get_value(property_key) # Get current data for the nested model field

        if nested_model_class and target_attr_name:
            # We have a recognized nested BaseModel field.
            # Conditionally render buttons or inline form.
            if self.split_nested_models:
                field_title = property_schema.get("title", property_key)
                cols = self.st.columns([3,1,1,1]) if current_value is not None and (property_key not in self._schema_required) else self.st.columns([3,1,1])
                with cols[0]:
                    st.markdown(f"**{field_title}**")
                    if current_value is not None:
                        st.json(current_value, expanded=False)
                    elif property_key not in self._schema_required:
                        st.caption("Optional field, not set.")
                    else:
                        st.caption("Required field, not set.")

                button_key_base = f"{self.key}-{self._session_state.run_id}-{target_attr_name}"

                if current_value is None:
                    if cols[1].button("➕ Create", key=f"{button_key_base}-create", help=f"Create {field_title}"):
                        #TODO: push editing context
                        pass
                else:
                    if cols[1].button("✏️ Edit", key=f"{button_key_base}-edit", help=f"Edit {field_title}"):
                        #TODO: push editing context
                        pass

                if current_value is not None and (property_key not in self._schema_required):
                    remove_button_col_idx = 2
                    if cols[remove_button_col_idx].button("➖ Remove", key=f"{button_key_base}-remove", help=f"Remove {field_title}"):
                        self._store_value(property_key, None)
                        st.rerun()
                return current_value # Buttons handle navigation
            else:
                # Fallback for pydantic_form: render inline (original behavior)
                return self.renderer.render_single_object_input(self.st, property_key, property_schema)

        else: # Not a recognized nested BaseModel or type extraction failed, fallback
            # Fallback to original rendering for objects if type inspection fails
            return self.renderer.render_single_object_input(self.st, property_key, property_schema)

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

    def _store_value(self, key: str, value: Any) -> None:
        return self._store_value_in_state(
            self._session_state[self._session_input_key], key, value
        )

    def _get_value(self, key: str) -> Any:
        return self._get_value_from_state(
            self._session_state[self._session_input_key], key
        )