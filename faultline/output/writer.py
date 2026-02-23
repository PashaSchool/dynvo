import json
from pathlib import Path

from faultline.models.types import FeatureMap


def write_feature_map(feature_map: FeatureMap, output_path: str | None = None) -> str:
    """
    Writes the feature map to a JSON file.
    Returns the path where the file was saved.
    """
    if output_path is None:
        output_path = ".faultline/feature-map.json"

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = feature_map.model_dump(mode="json")
    path.write_text(json.dumps(data, indent=2, default=str))

    return str(path)
