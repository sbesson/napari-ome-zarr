"""This module is a napari plugin.

It implements the ``napari_get_reader`` hook specification, (to create a reader plugin).
"""


import logging
import warnings
from typing import Any, Callable, Dict, Iterator, List, Optional

import numpy as np
from vispy.color import Colormap

from ome_zarr.io import parse_url
from ome_zarr.reader import Label, Node, Reader
from ome_zarr.types import LayerData, PathLike, ReaderFunction

try:
    from napari_plugin_engine import napari_hook_implementation
except ImportError:

    def napari_hook_implementation(
        func: Callable, *args: Any, **kwargs: Any
    ) -> Callable:
        return func


LOGGER = logging.getLogger("napari_ome_zarr.reader")

# NB: color for labels, colormap for images
METADATA_KEYS = ("name", "visible", "contrast_limits", "colormap",
                 "color", "metadata")

@napari_hook_implementation
def napari_get_reader(path: PathLike) -> Optional[ReaderFunction]:
    """Returns a reader for supported paths that include IDR ID.

    - URL of the form: https://uk1s3.embassy.ebi.ac.uk/idr/zarr/v0.1/ID.zarr/
    """
    if isinstance(path, list):
        if len(path) > 1:
            warnings.warn("more than one path is not currently supported")
        path = path[0]
    zarr = parse_url(path)
    if zarr:
        reader = Reader(zarr)
        return transform(reader())
    # Ignoring this path
    return None


def transform_properties(props=None):
    """
    Transform properties

    Transform a dict of {label_id : {key: value, key2: value2}}
    with a key for every LABEL
    into a dict of a key for every VALUE, with a list of values for each
    {
        "index": [1381342, 1381343...]
        "omero:roiId": [1381342, 1381343...],
        "omero:shapeId": [1682567, 1682567...]
    }
    """
    if props is None:
        return None

    properties: Dict[str, List] = {}

    # First, create lists for all existing keys...
    for label_id, props_dict in props.items():
        for key in props_dict.keys():
            properties[key] = []

    keys = list(properties.keys())

    properties["index"] = []
    for label_id, props_dict in props.items():
        properties["index"].append(label_id)
        # ...in case some objects don't have all the keys
        for key in keys:
            properties[key].append(props_dict.get(key, None))
    return properties


def transform_scale(node_metadata, metadata, channel_axis, shape):
    """
    e.g. transformation is {"axisIndices": [1, 2, 3], "scale": [0.2, 0.06, 0.06]}
    Get a list of these for each level in data. Just use first?
    """
    if "transformations" in node_metadata:
        level_0_transforms = node_metadata["transformations"][0]
        for transf in level_0_transforms:
            if "scale" in transf and "axisIndices" in transf:
                axis_indices = transf["axisIndices"]
                scale = transf["scale"]
                scale_by_axis = {}
                for axis, scale_val in zip(axis_indices, scale):
                    scale_by_axis[axis] = scale_val
                # for each dimension of the data (not including channels), we want
                # scale value, or 1 if not found
                scale_values = []
                for dim in range(len(shape)):
                    if dim != channel_axis:
                        scale_values.append(scale_by_axis.get(dim, 1))
                if len(scale_values) > 0:
                    metadata["scale"] = tuple(scale_values)


def transform(nodes: Iterator[Node]) -> Optional[ReaderFunction]:
    def f(*args: Any, **kwargs: Any) -> List[LayerData]:
        results: List[LayerData] = list()

        for node in nodes:
            data: List[Any] = node.data
            metadata: Dict[str, Any] = {}
            if data is None or len(data) < 1:
                LOGGER.debug(f"skipping non-data {node}")
            else:
                LOGGER.debug(f"transforming {node}")

                layer_type: str = "image"
                channel_axis = None
                try:
                    ch_types = [axis["type"] for axis in node.metadata["axes"]]
                    if "channel" in ch_types:
                        channel_axis = ch_types.index("channel")
                except:
                    LOGGER.error("Error reading axes: Please update ome-zarr")
                    raise

                transform_scale(node.metadata, metadata, channel_axis, data[0].shape)
                # If layer has no scale info, try apply scale from first layer
                if "scale" not in metadata and len(results) and "scale" in results[0][1]:
                    # e.g. labels layer should be scaled to match the image
                    metadata["scale"] = results[0][1]["scale"]

                if node.load(Label):
                    layer_type = "labels"
                    for x in METADATA_KEYS:
                        if x in node.metadata:
                            metadata[x] = node.metadata[x]
                    if channel_axis is not None:
                        data = [np.squeeze(level, axis=channel_axis) for level in node.data]
                else:
                    LOGGER.debug("node.metadata: %s" % node.metadata)
                    # Handle the removal of vispy requirement from ome-zarr-py
                    cms = node.metadata.get("colormap", [])
                    for idx, cm in enumerate(cms):
                        if not isinstance(cm, Colormap):
                            cms[idx] = Colormap(cm)

                    if channel_axis is not None:
                        # multi-channel; Copy known metadata values
                        metadata["channel_axis"] = channel_axis
                        for x in METADATA_KEYS:
                            if x in node.metadata:
                                metadata[x] = node.metadata[x]
                    else:
                        # single channel image, so metadata just needs single items (not lists)
                        for x in METADATA_KEYS:
                            if x in node.metadata:
                                try:
                                    metadata[x] = node.metadata[x][0]
                                except Exception:
                                    pass

                properties = transform_properties(node.metadata.get("properties"))
                if properties is not None:
                    metadata["properties"] = properties

                rv: LayerData = (data, metadata, layer_type)
                LOGGER.debug(f"Transformed: {rv}")
                results.append(rv)

        return results

    return f
