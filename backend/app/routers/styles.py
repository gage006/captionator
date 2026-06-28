from fastapi import APIRouter
from ..styles.definitions import STYLES, base_font_size
from ..schemas import StyleInfo

router = APIRouter()


@router.get("/styles", response_model=list[StyleInfo])
def list_styles():
    return [
        StyleInfo(
            id=style_id,
            label=data["label"],
            description=data["description"],
            preview_color=data["preview_color"],
            base_font_size=base_font_size(data),
        )
        for style_id, data in STYLES.items()
    ]
