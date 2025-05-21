import base64
import contextlib
import datetime
import mimetypes

from typing import Any, Optional, Union
from pydantic_extra_types.color import Color

from streamlit_pydantic import schema_utils
from streamlit_pydantic.v2.streamlit_base import StreamlitBase
from streamlit_pydantic.v2.utils import _is_compatible_audio, _is_compatible_image, _is_compatible_video, _name_to_title

class StreamlitRenderer(StreamlitBase):

    def __init__(self, run_id: int, key: str, schema_references: dict, lowercase_labels: bool = False):
        super().__init__(run_id, key, schema_references, lowercase_labels=lowercase_labels)

    def render_property(self,
                        st_app,
                        property_key,
                        property_schema,
                        data: Optional[Union[dict, list]] = None):
        # filter the case of optional and nullable
        property = schema_utils.filter_nullable(property_schema)
        if schema_utils.is_single_enum_property(property, self._schema_references):
            return self.render_single_enum_input(st_app, property_key, property, data=data)

        if schema_utils.is_multi_enum_property(property, self._schema_references):
            return self.render_multi_enum_input(st_app, property_key, property, data=data)

        if schema_utils.is_single_file_property(property):
            return self.render_single_file_input(st_app, property_key, property)

        if schema_utils.is_multi_file_property(property):
            raise NotImplementedError

        if schema_utils.is_single_datetime_property(property):
            return self.render_single_datetime_input(st_app, property_key, property, data=data)

        if schema_utils.is_single_color_property(property):
            return self.render_single_color_input(st_app, property_key, property, data=data)

        if schema_utils.is_single_boolean_property(property):
            return self.render_single_boolean_input(st_app, property_key, property, data=data)

        if schema_utils.is_single_dict_property(property):
            return self.render_single_dict_input(st_app, property_key, property, data)

        if schema_utils.is_single_number_property(property):
            return self.render_single_number_input(st_app, property_key, property, data=data)

        if schema_utils.is_single_string_property(property):
            return self.render_single_string_input(st_app, property_key, property, data=data)

        if schema_utils.is_single_object(property, self._schema_references):
            return self.render_single_object_input(st_app, property_key, property)

        if schema_utils.is_object_list_property(property, self._schema_references):
            return self.render_list_input(st_app, property_key, property, data)

        if schema_utils.is_property_list(property):
            return self.render_list_input(st_app, property_key, property, data)

        if schema_utils.is_single_reference(property):
            raise NotImplementedError
            #TODO: this is the part of the logic that we want to improve. Instead of just adding the child to the UI, create a new window for it

        if schema_utils.is_union_property(property):
            return self.render_union_property(st_app, property_key, property)

        st_app.warning(
            "The type of the following property is currently not supported: "
            + str(property.get("title"))
        )
        raise Exception("Unsupported property")

    def render_single_enum_input(
        self, streamlit_app: Any, key: str, property: dict, data: Any = None
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        select_options: list[str] = []
        if property.get("enum"):
            select_options = property.get("enum")  # type: ignore
        else:
            reference_item = schema_utils.get_single_reference_item(
                property, self._schema_references
            )
            select_options = reference_item["enum"]

        initial_selection = None
        if data is not None:
            initial_selection = data
        elif property.get("init_value"):
            initial_selection = property.get("init_value")
        elif property.get("default") is not None:
            initial_selection = property.get("default")
        
        if initial_selection is not None and initial_selection in select_options:
            try:
                streamlit_kwargs["index"] = select_options.index(initial_selection)
            except ValueError:
                pass 
        elif not select_options:
            return streamlit_app.selectbox(**{**streamlit_kwargs, "options": [], "disabled": True, **overwrite_kwargs})

        if len(select_options) == 1:
            return select_options[0]
        else:
            return streamlit_app.selectbox(
                **{**streamlit_kwargs, "options": select_options, **overwrite_kwargs}
            )
        
    def render_multi_enum_input(
        self, streamlit_app: Any, key: str, property: dict, data: Any = None
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        select_options: list[str] = []
        if property.get("items").get("enum"):  # type: ignore
            select_options = property.get("items").get("enum")  # type: ignore
        else:
            reference_item = schema_utils.resolve_reference(
                property["items"]["$ref"], self._schema_references
            )
            select_options = reference_item["enum"]

        default_value = []
        if data is not None:
            if isinstance(data, list):
                default_value = [opt for opt in data if opt in select_options]
            elif data not in select_options:
                pass
        elif property.get("init_value"):
            if isinstance(property.get("init_value"), list):
                default_value = [opt for opt in property.get("init_value") if opt in select_options]
        elif property.get("default"):
            if isinstance(property.get("default"), list):
                default_value = [opt for opt in property.get("default") if opt in select_options]
        
        streamlit_kwargs["default"] = default_value

        return streamlit_app.multiselect(
            **{**streamlit_kwargs, "options": select_options, **overwrite_kwargs}
        )
    
    def render_single_file_input(
        self, streamlit_app: Any, key: str, property: dict
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

    def render_single_boolean_input(
        self, streamlit_app: Any, key: str, property: dict, data: Any = None
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        if data is not None:
            streamlit_kwargs["value"] = bool(data)
        elif "init_value" in property:
            streamlit_kwargs["value"] = property.get("init_value")
        elif "default" in property:
            streamlit_kwargs["value"] = property.get("default")

        if property.get("is_item"):
            streamlit_app.markdown("##")

        return streamlit_app.checkbox(**{**streamlit_kwargs, **overwrite_kwargs})

    def render_single_number_input(
        self, streamlit_app: Any, key: str, property: dict, data: Any = None
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

        initial_value_to_set = None
        if data is not None:
            try:
                initial_value_to_set = number_transform(data)
            except (ValueError, TypeError):
                self.st.warning(f"Invalid data '{data}' for number input '{key}'. Using schema defaults.")
                pass

        if initial_value_to_set is None:
            if property.get("init_value") is not None:
                initial_value_to_set = number_transform(property["init_value"])
            elif property.get("default") is not None:
                initial_value_to_set = number_transform(property["default"])  # type: ignore
            else:
                if "min_value" in streamlit_kwargs:
                    initial_value_to_set = streamlit_kwargs["min_value"]
                elif number_transform == int:
                    initial_value_to_set = 0
                else:
                    # Set default value to step, assuming 'step' is already in streamlit_kwargs
                    initial_value_to_set = number_transform(
                        streamlit_kwargs["step"]
                    )
        streamlit_kwargs["value"] = initial_value_to_set

        if "min_value" in streamlit_kwargs and "max_value" in streamlit_kwargs:
            # TODO: Only if less than X steps
            return streamlit_app.slider(**{**streamlit_kwargs, **overwrite_kwargs})
        else:
            return streamlit_app.number_input(
                **{**streamlit_kwargs, **overwrite_kwargs}
            )

    def render_object_input(self, streamlit_app: Any, key: str, property: dict) -> Any:
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

            value = self.render_property(streamlit_app, full_key, new_property)
            if not self._is_value_ignored(property_key, value):
                object_inputs[property_key] = value

        return object_inputs

    def render_single_object_input(
        self, streamlit_app: Any, key: str, property: dict
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

        return self.render_object_input(streamlit_app, key, object_reference)

    def render_list_item(
        self,
        streamlit_app: Any,
        parent_key: str,
        value: Any,
        index: int,
        property: dict[str, Any],
    ) -> Any:
        label = "Item #" + str(index + 1)
        new_key = self._key + "-" + parent_key + "." + str(index)
        item_placeholder = streamlit_app.empty()

        with item_placeholder:
            input_col, button_col = streamlit_app.columns([8, 3])

            button_col.markdown("##")

            if self.remove_button_allowed(index, property):
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
                    return self.render_property(streamlit_app, new_key, new_property)

            else:
                # when the remove button is clicked clear the placeholder and return None
                item_placeholder.empty()
                return None

    def render_dict_item(
        self,
        streamlit_app: Any,
        parent_key: str,
        in_value: tuple[str, Any],
        index: int,
        property: dict[str, Any],
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

            if self.remove_button_allowed(index, property):
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
                    value_property_schema = {
                        "title": "Value",
                        "is_item": True,
                        "readOnly": property.get("readOnly"),
                        **property["additionalProperties"],
                    }
                    updated_value = self.render_property(
                        streamlit_app, 
                        dict_value_key, 
                        value_property_schema, 
                        data=dict_value 
                    )

                    return updated_key, updated_value

            else:
                # when the remove button is clicked clear the placeholder and return None
                item_placeholder.empty()
                return None, None
            
    def render_single_datetime_input(
        self, streamlit_app: Any, key: str, property: dict, data: Any = None
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        def parse_to_type(value_to_parse: Any, dt_type: str) -> Any:
            if value_to_parse is None: return None
            if dt_type == "time" and isinstance(value_to_parse, datetime.time): return value_to_parse
            if dt_type == "date" and isinstance(value_to_parse, datetime.date): return value_to_parse
            if dt_type == "date-time" and isinstance(value_to_parse, datetime.datetime): return value_to_parse
            
            try:
                if isinstance(value_to_parse, str):
                    if dt_type == "time": return datetime.time.fromisoformat(value_to_parse)
                    if dt_type == "date": return datetime.date.fromisoformat(value_to_parse)
                    if dt_type == "date-time": return datetime.datetime.fromisoformat(value_to_parse)
            except ValueError:
                return None
            return None

        target_format = property.get("format")
        if data is not None:
            parsed_data = parse_to_type(data, target_format)
            if parsed_data is not None:
                 streamlit_kwargs["value"] = parsed_data

        if "value" not in streamlit_kwargs:
            schema_default_value = property.get("init_value") if property.get("init_value") is not None else property.get("default")
            if schema_default_value is not None:
                parsed_schema_default = parse_to_type(schema_default_value, target_format)
                if parsed_schema_default is not None:
                    streamlit_kwargs["value"] = parsed_schema_default

        if target_format == "time":
            return streamlit_app.time_input(**{**streamlit_kwargs, **overwrite_kwargs})
        elif target_format == "date":
            return streamlit_app.date_input(**{**streamlit_kwargs, **overwrite_kwargs})
        elif target_format == "date-time":
            with streamlit_app.container():
                if not property.get("is_item"):
                    streamlit_app.subheader(streamlit_kwargs.get("label"))
                if streamlit_kwargs.get("description"):
                    streamlit_app.text(streamlit_kwargs.get("description"))
                selected_date = None
                selected_time = None

                if property.get("is_item"):
                    date_col = streamlit_app.container()
                    time_col = streamlit_app.container()
                else:
                    date_col, time_col = streamlit_app.columns(2)
                with date_col:
                    date_kwargs = {**{**streamlit_kwargs, **overwrite_kwargs}}
                    date_kwargs["label"] = "Date"
                    date_kwargs["key"] = (f"{streamlit_kwargs.get('key')}-date-input",)

                    value = streamlit_kwargs.get("value")
                    if value:
                        with contextlib.suppress(Exception):
                            date_kwargs["value"] = value.date()
                    selected_date = streamlit_app.date_input(**date_kwargs)

                with time_col:
                    time_kwargs = {**{**streamlit_kwargs, **overwrite_kwargs}}
                    time_kwargs["label"] = "Time"
                    time_kwargs["key"] = f"{streamlit_kwargs.get('key')}-time-input"

                    value = streamlit_kwargs.get("value")
                    if value:
                        with contextlib.suppress(Exception):
                            time_kwargs["value"] = value.time()
                    selected_time = streamlit_app.time_input(**time_kwargs)

                return datetime.datetime.combine(selected_date, selected_time)
        else:
            streamlit_app.warning(
                "Date format is not supported: " + str(property.get("format"))
            )

    def render_single_color_input(
        self, streamlit_app: Any, key: str, property: dict, data: Any = None
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)
        
        value_to_use = None
        if data is not None:
            value_to_use = data
        elif property.get("init_value") is not None:
            value_to_use = property["init_value"]
        elif property.get("default") is not None:
            value_to_use = property["default"]
        elif property.get("example") is not None:
            value_to_use = property["example"]
        
        if value_to_use is not None:
            if isinstance(value_to_use, Color):
                streamlit_kwargs["value"] = value_to_use.as_hex()
            elif isinstance(value_to_use, str):
                try:
                    streamlit_kwargs["value"] = Color(value_to_use).as_hex()
                except ValueError:
                    pass

        if property.get("format") == "text":
            return streamlit_app.text_input(**{**streamlit_kwargs, **overwrite_kwargs})
        else:
            return streamlit_app.color_picker(
                **{**streamlit_kwargs, **overwrite_kwargs}
            )
        
    def render_single_string_input(
        self, streamlit_app: Any, key: str, property: dict, data: Any = None
    ) -> Any:
        streamlit_kwargs = self._get_default_streamlit_input_kwargs(key, property)
        overwrite_kwargs = self._get_overwrite_streamlit_kwargs(key, property)

        if data is not None:
            streamlit_kwargs["value"] = str(data)
        elif property.get("init_value"):
            streamlit_kwargs["value"] = property.get("init_value")
        elif property.get("default"):
            streamlit_kwargs["value"] = property.get("default")
        elif property.get("example"):
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
        
    def render_single_dict_input(
        self, streamlit_app: Any, key: str, property: dict, data_dict: Optional[dict] = None
    ) -> Any:
        # Add title and subheader
        streamlit_app.subheader(property.get("title"))
        if property.get("description"):
            streamlit_app.markdown(property.get("description"))

        if data_dict is None:
            if property.get("init_value"):
                data_dict = property.get("init_value")
            elif property.get("default"):
                data_dict = property.get("default")
            else:
                data_dict = {}

        is_object = True if property["additionalProperties"].get("$ref") else False

        add_col, clear_col, _ = streamlit_app.columns(3)

        add_col = add_col.empty()

        if self.clear_button_allowed(property):
            data_dict = self.render_dict_add_button(key, add_col, data_dict)

        if self.clear_button_allowed(property):
            data_dict = self.render_dict_clear_button(key, clear_col, data_dict)

        new_dict = {}

        for index, input_item in enumerate(data_dict.items()):
            updated_key, updated_value = self.render_dict_item(
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

    def add_button_allowed(
        self,
        index: int,
        property: dict[str, Any],
    ) -> bool:
        add_allowed = not (
            (property.get("readOnly", False) is True)
            or ((index) >= property.get("maxItems", 1000))
        )

        return add_allowed

    def remove_button_allowed(
        self,
        index: int,
        property: dict[str, Any],
    ) -> bool:
        remove_allowed = (property.get("readOnly") is True) or (
            (index + 1) <= property.get("minItems", 0)
        )

        return remove_allowed

    def clear_button_allowed(
        self,
        property: dict[str, Any],
    ) -> bool:
        clear_allowed = not (
            (property.get("readOnly", False) is True)
            or (property.get("minItems", 0) > 0)
        )

        return clear_allowed

    def render_list_add_button(
        self,
        key: str,
        streamlit_app: Any,
        data_list: list[Any],
    ) -> list[Any]:
        if streamlit_app.button(
            "Add Item",
            key=self._key + "-" + key + "list-add-item",
        ):
            data_list.append(None)

        return data_list

    def render_list_clear_button(
        self,
        key: str,
        streamlit_app: Any,
        data_list: list[Any],
    ) -> list[Any]:
        if streamlit_app.button(
            "Clear All",
            key=self._key + "_" + key + "-list_clear-all",
        ):
            data_list = []

        return data_list

    def render_dict_add_button(
        self, key: str, streamlit_app: Any, data_dict: dict[str, Any]
    ) -> dict[str, Any]:
        if streamlit_app.button(
            "Add Item",
            key=self._key + "-" + key + "-add-item",
        ):
            data_dict[str(len(data_dict) + 1)] = None

        return data_dict

    def render_dict_clear_button(
        self,
        key: str,
        streamlit_app: Any,
        data_dict: dict[str, Any],
    ) -> dict[str, Any]:
        if streamlit_app.button(
            "Clear All",
            key=self._key + "-" + key + "-clear-all",
        ):
            data_dict = {}

        return data_dict

    def render_list_input(self, streamlit_app: Any, key: str, property: dict, data_list: Optional[list] = None) -> Any:
        # Add title and subheader
        streamlit_app.subheader(property.get("title"))
        if property.get("description"):
            streamlit_app.markdown(property.get("description"))

        is_object = True if property["items"].get("$ref") else False

        object_list = []

        # Treat empty list as a session data "hit"
        if data_list is None:
            if property.get("init_value"):
                data_list = property.get("init_value")
            elif property.get("default"):
                data_list = property.get("default")
            else:
                data_list = []

        add_col, clear_col, _ = streamlit_app.columns(3)

        add_col = add_col.empty()

        self.render_list_add_button(key, add_col, data_list)

        if self.clear_button_allowed(property):
            data_list = self.render_list_clear_button(key, clear_col, data_list)

        if len(data_list) > 0:
            for index, item in enumerate(data_list):
                output = self.render_list_item(
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

            if not self.add_button_allowed(len(object_list), property):
                add_col = add_col.empty()

            if not is_object:
                streamlit_app.markdown("---")

        return object_list

    def render_union_property(
        self, streamlit_app: Any, key: str, property: dict
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

        name_reference_mapping: dict[str, dict] = {}

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

        input_data = self.render_object_input(
            streamlit_app, key, name_reference_mapping[selected_reference]
        )

        streamlit_app.markdown("---")
        return input_data