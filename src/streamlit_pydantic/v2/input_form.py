import inspect
from typing import Any, Dict, List, Optional, Type, Union, get_args, get_origin
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo
import streamlit as st
import copy

from streamlit_pydantic import schema_utils
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

    def _get_dict_value_model_class(self, field_info: FieldInfo) -> Optional[Type[BaseModel]]:
        """Checks if a dictionary field's values are Pydantic models."""
        # field_info.annotation should be something like Dict[KeyType, ValueType]
        origin_type = get_origin(field_info.annotation)
        if origin_type is dict:
            args = get_args(field_info.annotation)
            if len(args) == 2:
                value_type = args[1]
                # Value type could be a Union (e.g., Optional[MyModel])
                if get_origin(value_type) is Union:
                    for union_arg in get_args(value_type):
                        if inspect.isclass(union_arg) and issubclass(union_arg, BaseModel):
                            return union_arg
                elif inspect.isclass(value_type) and issubclass(value_type, BaseModel):
                    return value_type
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
            dict_value_model_class = self._get_dict_value_model_class(field_info)

            field_path_in_main_data = f"{data_access_path}.{actual_attr_name}" if not is_nested_model and data_access_path else actual_attr_name

            if dict_value_model_class and schema_utils.is_single_dict_property(prop_schema_value) and self.split_nested_models:
                self._render_dictionary_with_model_values(
                    property_schema_key=prop_schema_key,
                    property_attr_name=actual_attr_name,
                    dict_property_schema=prop_schema_value,
                    value_model_class=dict_value_model_class,
                    parent_model_class=model_class,
                    parent_data_access_path=data_access_path,
                    is_parent_editing_mode=is_nested_model
                )
            elif nested_model_class and self.split_nested_models:
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
                        prop_schema_value,
                        data=current_value
                    )
                else:
                    returned_value = self.renderer.render_property(
                        self.st, 
                        unique_key_for_renderer_call, 
                        prop_schema_value, 
                        data=current_value
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

    def _render_dictionary_with_model_values(
        self,
        property_schema_key: str,
        property_attr_name: str,
        dict_property_schema: dict,
        value_model_class: Type[BaseModel],
        parent_model_class: Type[BaseModel],
        parent_data_access_path: str,
        is_parent_editing_mode: bool
    ):
        dict_title = dict_property_schema.get("title", _name_to_title(property_attr_name))
        editing_stack: List[Dict[str, Any]] = self._session_state[self._editing_stack_session_key]

        current_dict_data: Optional[dict]
        path_to_dict_in_main_store: str

        if is_parent_editing_mode:
            current_dict_data = self._get_temp_editing_value(parent_data_access_path, property_attr_name)

            parent_context = next((item for item in reversed(editing_stack) if item["temp_data_id"] == parent_data_access_path), None)
            if not parent_context:
                self.st.error(f"Internal error: Parent editing context not found for {parent_data_access_path} while rendering dict '{property_attr_name}'.")
                return
            parent_save_path = parent_context['save_to_path']
            path_to_dict_in_main_store = f"{parent_save_path}.{property_attr_name}" if parent_save_path else property_attr_name
        else:
            path_to_dict_in_main_store = f"{parent_data_access_path}.{property_attr_name}" if parent_data_access_path else property_attr_name
            current_dict_data = self._get_main_data_value(path_to_dict_in_main_store)

        if current_dict_data is None:
            current_dict_data = dict_property_schema.get("default", {})
            if is_parent_editing_mode:
                self._store_temp_editing_value(parent_data_access_path, property_attr_name, current_dict_data)
            else:
                self._store_main_data_value(path_to_dict_in_main_store, current_dict_data)


        self.st.subheader(dict_title)
        if dict_property_schema.get("description"):
            self.st.markdown(dict_property_schema.get("description"))

        btn_cols = self.st.columns(2)
        action_on_dict = False

        dict_controls_key_base = f"{self.key}-dictctl-{parent_data_access_path}-{property_attr_name}"

        if btn_cols[0].button("➕ Add New Item to Dictionary", key=f"{dict_controls_key_base}-add", use_container_width=True):
            new_key_base = "new_key"
            new_key_suffix = 1
            candidate_new_key = f"{new_key_base}_{new_key_suffix}"
            while candidate_new_key in current_dict_data:
                new_key_suffix += 1
                candidate_new_key = f"{new_key_base}_{new_key_suffix}"
            current_dict_data[candidate_new_key] = None
            action_on_dict = True
            
        if btn_cols[1].button("Clear All Items", key=f"{dict_controls_key_base}-clear", use_container_width=True):
            current_dict_data.clear()
            action_on_dict = True

        if action_on_dict:
            if is_parent_editing_mode:
                self._store_temp_editing_value(parent_data_access_path, property_attr_name, current_dict_data)
            else:
                self._store_main_data_value(path_to_dict_in_main_store, current_dict_data)
            self.st.rerun()

        if not current_dict_data:
            self.st.caption("Dictionary is empty.")
        
        items_to_process = list(current_dict_data.items())
        
        for item_idx, (item_key, item_value) in enumerate(items_to_process):
            item_container = self.st.container()
            with item_container:
                item_key_base = f"{dict_controls_key_base}-item{item_idx}"
                
                new_item_key = item_key
                
                key_col, val_col, remove_item_col = item_container.columns([2,3,1])

                with key_col:
                    new_item_key = self.st.text_input(
                        "Key", 
                        value=item_key, 
                        key=f"{item_key_base}-keytext",
                    )

                key_changed = False
                if new_item_key != item_key:
                    if new_item_key in current_dict_data and new_item_key != item_key :
                        key_col.error(f"Key '{new_item_key}' already exists. Choose a unique key.")
                    else:
                        current_dict_data.pop(item_key)
                        current_dict_data[new_item_key] = item_value 
                        item_key = new_item_key
                        key_changed = True


                with val_col:
                    self.st.markdown(f"**Value for '{item_key}'** (`{value_model_class.__name__}`)")
                    
                    path_for_this_item_value_in_main_store = f"{path_to_dict_in_main_store}.{item_key}"
                    
                    if item_value is not None:
                        try:
                            self.st.json(item_value, expanded=False)
                        except:
                            self.st.text(str(item_value))
                    else:
                        self.st.caption("Value not set (None).")

                    val_action_cols = self.st.columns(3 if item_value is not None else 2)
                    value_action_taken = False

                    if item_value is None:
                        if val_action_cols[0].button("➕ Create Value", key=f"{item_key_base}-createval", use_container_width=True):
                            temp_data_id = f"temp_val_{property_attr_name}_{item_key}_{len(editing_stack)}_{self._session_state.run_id}"
                            try:
                                initial_data = value_model_class().model_dump(by_alias=True)
                            except Exception:
                                initial_data = {}
                            self._session_state[self._session_temp_editing_data_key][temp_data_id] = initial_data
                            editing_stack.append({
                                "model_class": value_model_class,
                                "temp_data_id": temp_data_id,
                                "save_to_path": path_for_this_item_value_in_main_store,
                                "title": f"Create value for '{item_key}' in '{dict_title}'"
                            })
                            value_action_taken = True
                    else:
                        if val_action_cols[0].button("✏️ Edit Value", key=f"{item_key_base}-editval", use_container_width=True):
                            temp_data_id = f"temp_val_{property_attr_name}_{item_key}_{len(editing_stack)}_{self._session_state.run_id}"
                            self._session_state[self._session_temp_editing_data_key][temp_data_id] = copy.deepcopy(item_value)
                            editing_stack.append({
                                "model_class": value_model_class,
                                "temp_data_id": temp_data_id,
                                "save_to_path": path_for_this_item_value_in_main_store,
                                "title": f"Edit value for '{item_key}' in '{dict_title}'"
                            })
                            value_action_taken = True
                        
                        if val_action_cols[1].button("🗑️ Clear Value", key=f"{item_key_base}-clearval", help="Set this value to None", use_container_width=True):
                            current_dict_data[item_key] = None
                            value_action_taken = True


                item_removed_from_dict = False
                with remove_item_col:
                    self.st.markdown("## ")
                    if self.st.button("❌ Remove Item", key=f"{item_key_base}-removeitem", help=f"Remove entry for key '{item_key}'", use_container_width=True):
                        current_dict_data.pop(item_key)
                        item_removed_from_dict = True
                
                self.st.markdown("---")

                if key_changed or value_action_taken or item_removed_from_dict:
                    if is_parent_editing_mode:
                        self._store_temp_editing_value(parent_data_access_path, property_attr_name, current_dict_data)
                    else:
                        self._store_main_data_value(path_to_dict_in_main_store, current_dict_data)
                    self.st.rerun()