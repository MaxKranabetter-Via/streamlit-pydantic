from typing import Any


_OVERWRITE_STREAMLIT_KWARGS_PREFIX = "st_kwargs_"

class StreamlitBase:

    def __init__(self, run_id: int, key: str, schema_references: dict, lowercase_labels: bool = False):
        self._run_id = run_id
        self._key = key
        self._schema_references = schema_references
        self._lowercase_labels = lowercase_labels

    def _get_default_streamlit_input_kwargs(self, key: str, property: dict) -> dict:
        label: str = property.get("title")
        if label and self._lowercase_labels:
            label = label.lower()

        disabled = False
        if property.get("readOnly"):
            # Read only property -> only show value
            disabled = True

        streamlit_kwargs = {
            "label": label,
            "key": str(self._run_id) + "-" + str(self._key) + "-" + key,
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
    
    def _get_overwrite_streamlit_kwargs(self, key: str, property: dict[str, Any]) -> dict[str, Any]:
        streamlit_kwargs: dict = {}

        for kwarg in property:
            if kwarg.startswith(_OVERWRITE_STREAMLIT_KWARGS_PREFIX):
                streamlit_kwargs[
                    kwarg.replace(_OVERWRITE_STREAMLIT_KWARGS_PREFIX, "")
                ] = property[kwarg]
        return streamlit_kwargs