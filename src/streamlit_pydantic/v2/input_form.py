import inspect
from typing import Any, Dict, List, Optional, Type, Union, get_args, get_origin
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo
import streamlit as st

from streamlit_pydantic.v2.ui_renderer_v2 import StreamlitRenderer
from streamlit_pydantic.v2.utils import _name_to_title, is_single_object


class InputUI:

    def __init__(self, key: str, model: Type[BaseModel], streamlit_container: Any = st, initial_data_object: Optional[BaseModel] = None):
        self.key = key
        self.root_model_class = model
        self.st = streamlit_container
        self.split_nested_models = True

        self._session_state = st.session_state
        if "run_id" not in self._session_state:
            self._session_state.run_id = 0

        self._session_main_data_key = f"{self.key}-main-data"
        if self._session_main_data_key not in self._session_state:
            if initial_data_object is not None:
                if isinstance(initial_data_object, self.root_model_class):
                    try:
                        self._session_state[self._session_main_data_key] = initial_data_object.model_dump(by_alias=True)
                        self.st.info(f"Form '{key}' pre-loaded with data from the provided object.")
                    except Exception as e:
                        self.st.error(f"Error dumping provided initial_data_object for form '{key}': {e}. Initializing with empty data.")
                        self._session_state[self._session_main_data_key] = {}
                else:
                    self.st.warning(
                        f"The provided initial_data_object for form '{key}' is not an instance of "
                        f"{self.root_model_class.__name__}. Initializing with default data for {self.root_model_class.__name__}."
                    )
                    # Fallback to default model initialization
                    try:
                        self._session_state[self._session_main_data_key] = self.root_model_class().model_dump(by_alias=True)
                    except Exception:
                        self._session_state[self._session_main_data_key] = {}
            else:
                # No initial_data_object provided, use default initialization
                try:
                    self._session_state[self._session_main_data_key] = self.root_model_class().model_dump(by_alias=True)
                except Exception:
                    self._session_state[self._session_main_data_key] = {}

        self._editing_stack_session_key = f"{self.key}-pydantic-editing-stack"
        if self._editing_stack_session_key not in self._session_state:
            self._session_state[self._editing_stack_session_key] = []

        self._session_temp_editing_data_key = f"{self.key}-temp-editing-data"
        if self._session_temp_editing_data_key not in self._session_state:
            self._session_state[self._session_temp_editing_data_key] = {}
        
        self._schema_cache = {}

        root_schema = self._get_model_schema(self.root_model_class)
        self.renderer = StreamlitRenderer(
            run_id=self._session_state.run_id,
            key=self.key, 
            schema_references=root_schema.get("$defs", {})
        )

    def _get_model_schema(self, model_class: Type[BaseModel]) -> dict:
        if model_class not in self._schema_cache:
            self._schema_cache[model_class] = model_class.model_json_schema(by_alias=True)
        return self._schema_cache[model_class]

    def _get_value_from_state(self, state_dict: dict, key: str) -> Any:
        current = state_dict
        if not key:
            return current
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None 
        return current

    def _store_value_in_state(self, state_dict: dict, key: str, value: Any) -> None:
        parts = key.split(".")
        current = state_dict
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                current[part] = value
            else:
                if part not in current or not isinstance(current[part], dict):
                    current[part] = {}
                current = current[part]
    
    def _delete_value_in_state(self, state_dict: dict, key: str) -> None:
        parts = key.split(".")
        current = state_dict
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                if isinstance(current, dict) and part in current:
                    del current[part]
                return
            else:
                if not isinstance(current, dict) or part not in current:
                    return 
                current = current[part]

    def _get_main_data_value(self, key: str) -> Any:
        return self._get_value_from_state(self._session_state[self._session_main_data_key], key)

    def _store_main_data_value(self, key: str, value: Any) -> None:
        self._store_value_in_state(self._session_state[self._session_main_data_key], key, value)

    def _get_temp_editing_value(self, temp_data_id: str, sub_key: str) -> Any:
        if temp_data_id not in self._session_state[self._session_temp_editing_data_key]:
            self._session_state[self._session_temp_editing_data_key][temp_data_id] = {}
        return self._get_value_from_state(self._session_state[self._session_temp_editing_data_key][temp_data_id], sub_key)

    def _store_temp_editing_value(self, temp_data_id: str, sub_key: str, value: Any) -> None:
        if temp_data_id not in self._session_state[self._session_temp_editing_data_key]:
            self._session_state[self._session_temp_editing_data_key][temp_data_id] = {}
        self._store_value_in_state(self._session_state[self._session_temp_editing_data_key][temp_data_id], sub_key, value)
    
    def _clear_temp_editing_data(self, temp_data_id: str) -> None:
        if temp_data_id in self._session_state[self._session_temp_editing_data_key]:
            del self._session_state[self._session_temp_editing_data_key][temp_data_id]

    def _is_field_pydantic_model(self, field_info: FieldInfo) -> Optional[Type[BaseModel]]:
        actual_type = field_info.annotation
        
        candidate_types = [] 

        origin = get_origin(actual_type)
        if origin is Union: # Handles Union and Optional (e.g. Union[T, NoneType])
            for arg_type in get_args(actual_type):
                # We are interested in concrete types from the Union that could be BaseModels
                if arg_type is not type(None) and inspect.isclass(arg_type): # Ensure arg_type is a class and not NoneType
                    candidate_types.append(arg_type)
        elif actual_type is not type(None) and inspect.isclass(actual_type): # Ensure actual_type is a class and not NoneType
            # Not a Union, but a direct class type (e.g., MyModel, int, str)
            candidate_types.append(actual_type)
        # Otherwise (e.g., it's a generic like List[int], or Any, or ForwardRef)
        # candidate_types will be empty.

        for candidate_cls in candidate_types:
            # At this point, candidate_cls is confirmed by inspect.isclass() and is not NoneType.
            try:
                if issubclass(candidate_cls, BaseModel): # No need for inspect.isclass(candidate_cls) again here
                    return candidate_cls
            except TypeError:
                # This handles the reported error: if issubclass() itself fails
                # because candidate_cls is not considered a "class" by its strict standards,
                # despite inspect.isclass() being true for it.
                # We simply skip this candidate, treating it as not a BaseModel subclass.
                pass 
        
        return None

    def render_ui(self) -> Optional[BaseModel]:
        editing_stack: List[Dict[str, Any]] = self._session_state[self._editing_stack_session_key]

        if not editing_stack:
            self.st.subheader(f"Configure {self.root_model_class.__name__}") 
            self._render_form_for_model(
                model_class=self.root_model_class,
                data_access_path="", 
                is_nested_model=False
            )
            if self.st.button("Submit Root Form", key=f"{self.key}-submit-root"):
                try:
                    current_data = self._get_main_data_value("")
                    if current_data is None: current_data = {}
                    validated_data = self.root_model_class(**current_data)
                    self.st.success("Root form submitted successfully!")
                    self.st.json(validated_data.model_dump(by_alias=True))
                    return validated_data
                except ValidationError as e:
                    self.st.error(f"Validation Error: {e}")
                except Exception as ex:
                    self.st.error(f"Error submitting root form: {ex}")

            return None 
        else:
            current_edit_context = editing_stack[-1]
            model_class_to_edit = current_edit_context["model_class"]
            temp_data_id = current_edit_context["temp_data_id"]
            form_title = current_edit_context["title"]
            
            self.st.subheader(form_title)
            self._render_form_for_model(
                model_class=model_class_to_edit,
                data_access_path=temp_data_id, 
                is_nested_model=True
            )

            cols = self.st.columns(2)
            if cols[0].button("💾 Save", key=f"{self.key}-save-{temp_data_id}"):
                try:
                    current_temp_data = self._session_state[self._session_temp_editing_data_key].get(temp_data_id, {})
                    validated_instance = model_class_to_edit(**current_temp_data)
                    
                    save_to_path_in_main_data = current_edit_context["save_to_path"]
                    self._store_main_data_value(save_to_path_in_main_data, validated_instance.model_dump(by_alias=True))
                    
                    self._clear_temp_editing_data(temp_data_id)
                    editing_stack.pop()
                    self.st.rerun()
                except ValidationError as e:
                    self.st.error(f"Validation Error: {e}")
                except Exception as e:
                    self.st.error(f"An unexpected error occurred on save: {e}")


            if cols[1].button("❌ Cancel", key=f"{self.key}-cancel-{temp_data_id}"):
                self._clear_temp_editing_data(temp_data_id)
                editing_stack.pop()
                self.st.rerun()
            return None


    def _render_form_for_model(self, model_class: Type[BaseModel], data_access_path: str, is_nested_model: bool):
        schema = self._get_model_schema(model_class)
        properties = schema.get("properties", {})

        for prop_schema_key, prop_schema_value in properties.items():
            field_info = None
            actual_attr_name = prop_schema_key 
            
            for attr_name_iter, field_info_iter in model_class.model_fields.items():
                if field_info_iter.alias == prop_schema_key:
                    field_info = field_info_iter
                    actual_attr_name = attr_name_iter
                    break
                if not field_info and attr_name_iter == prop_schema_key: 
                    field_info = field_info_iter
                    actual_attr_name = attr_name_iter
            
            if not field_info and prop_schema_key in model_class.model_fields:
                actual_attr_name = prop_schema_key
                field_info = model_class.model_fields[prop_schema_key]

            if not field_info:
                continue

            if not prop_schema_value.get("title"):
                prop_schema_value["title"] = _name_to_title(actual_attr_name)

            nested_model_class = self._is_field_pydantic_model(field_info)

            field_path_in_main_data = f"{data_access_path}.{actual_attr_name}" if not is_nested_model and data_access_path else actual_attr_name

            if nested_model_class and self.split_nested_models:
                self._render_nested_model_controls(
                    property_key_in_schema=prop_schema_key,
                    property_attr_name=actual_attr_name,
                    property_schema=prop_schema_value,
                    parent_model_class=model_class,
                    parent_data_access_path=data_access_path, 
                    is_parent_editing_mode=is_nested_model,
                    nested_model_class=nested_model_class
                )
            else:
                current_value: Any
                if is_nested_model:
                    current_value = self._get_temp_editing_value(data_access_path, actual_attr_name)
                else:
                    current_value = self._get_main_data_value(field_path_in_main_data)
                
                unique_key_for_renderer_call = f"{self.key}_{'edit' if is_nested_model else 'main'}_{data_access_path}_{actual_attr_name}"

                returned_value: Any
                current_model_schema_defs = schema.get('$defs', {})
                if is_single_object(prop_schema_value, current_model_schema_defs) and not nested_model_class:
                    if unique_key_for_renderer_call not in st.session_state or st.session_state[unique_key_for_renderer_call] is None:
                         st.session_state[unique_key_for_renderer_call] = current_value

                    returned_value = self.renderer.render_single_object_input(
                        self.st,
                        unique_key_for_renderer_call, 
                        prop_schema_value
                    )
                else:
                    returned_value = self.renderer.render_property(
                        self.st, 
                        unique_key_for_renderer_call, 
                        prop_schema_value, 
                        current_value
                    )
                
                if current_value != returned_value:
                    if is_nested_model:
                        self._store_temp_editing_value(data_access_path, actual_attr_name, returned_value)
                    else:
                        self._store_main_data_value(field_path_in_main_data, returned_value)
                        st.rerun()
                                
    def _render_nested_model_controls(self, property_key_in_schema: str, property_attr_name: str, property_schema: dict,
                                      parent_model_class: Type[BaseModel], parent_data_access_path: str,
                                      is_parent_editing_mode: bool, nested_model_class: Type[BaseModel]):
        
        field_title = property_schema.get("title", _name_to_title(property_attr_name))
        editing_stack: List[Dict[str, Any]] = self._session_state[self._editing_stack_session_key]
        
        path_in_main_data_for_nested_model: str
        if is_parent_editing_mode:
            parent_context = next((item for item in reversed(editing_stack) if item["temp_data_id"] == parent_data_access_path), None)
            if not parent_context:
                self.st.error(f"Internal error: Parent editing context not found for {parent_data_access_path}")
                return
            parent_save_path = parent_context['save_to_path']
            path_in_main_data_for_nested_model = f"{parent_save_path}.{property_attr_name}" if parent_save_path else property_attr_name
        else:
            path_in_main_data_for_nested_model = f"{parent_data_access_path}.{property_attr_name}" if parent_data_access_path else property_attr_name

        current_value_in_main_data = self._get_main_data_value(path_in_main_data_for_nested_model)
        
        parent_schema = self._get_model_schema(parent_model_class)
        is_required_in_parent = property_key_in_schema in parent_schema.get("required", [])

        cols_spec = [3,1,1,1] if current_value_in_main_data is not None and not is_required_in_parent else [3,1,1]
        cols = self.st.columns(cols_spec)

        with cols[0]:
            self.st.markdown(f"**{field_title}**")
            if current_value_in_main_data is not None:
                self.st.json(current_value_in_main_data, expanded=False)
            elif not is_required_in_parent:
                self.st.caption("Optional field, not set.")
            else:
                self.st.caption("Required field, not set.")

        button_key_base = f"{self.key}-{self._session_state.run_id}-nestedctl-{parent_data_access_path}-{property_attr_name}"

        if current_value_in_main_data is None:
            if cols[1].button("➕ Create", key=f"{button_key_base}-create", help=f"Create {field_title}"):
                temp_data_id = f"temp_{property_attr_name}_{len(editing_stack)}_{self._session_state.run_id}"
                try:
                    initial_data = nested_model_class().model_dump(by_alias=True)
                except Exception: 
                    initial_data = {}
                self._session_state[self._session_temp_editing_data_key][temp_data_id] = initial_data
                
                editing_stack.append({
                    "model_class": nested_model_class,
                    "temp_data_id": temp_data_id,
                    "save_to_path": path_in_main_data_for_nested_model, 
                    "title": f"Create {field_title}"
                })
                self.st.rerun()
        else:
            if cols[1].button("✏️ Edit", key=f"{button_key_base}-edit", help=f"Edit {field_title}"):
                temp_data_id = f"temp_{property_attr_name}_{len(editing_stack)}_{self._session_state.run_id}"
                import copy
                self._session_state[self._session_temp_editing_data_key][temp_data_id] = copy.deepcopy(current_value_in_main_data)
                
                editing_stack.append({
                    "model_class": nested_model_class,
                    "temp_data_id": temp_data_id,
                    "save_to_path": path_in_main_data_for_nested_model,
                    "title": f"Edit {field_title}"
                })
                self.st.rerun()

        if current_value_in_main_data is not None and not is_required_in_parent:
            remove_button_col_idx = 2
            if cols[remove_button_col_idx].button("➖ Remove", key=f"{button_key_base}-remove", help=f"Remove {field_title}"):
                self._store_main_data_value(path_in_main_data_for_nested_model, None) 
                self.st.rerun()