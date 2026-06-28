# ASS color format: &H{AA}{BB}{GG}{RR}  (alpha, blue, green, red — all hex)
# &H00FFFFFF = opaque white, &H00000000 = opaque black, &H96000000 = 60% transparent black


def base_font_size(style: dict) -> int:
    """Return the Fontsize field (index 2) of a style's ASS line.

    Exposed to the frontend so the preview overlay can size caption text in
    proportion to the burned output (sizes are in PlayResY = video-height units).
    """
    try:
        return int(float(style["ass_style"].split(",")[2]))
    except (KeyError, IndexError, ValueError):
        return 48


STYLES: dict[str, dict] = {
    "classic": {
        "label": "Classic",
        "description": "White text, black outline, bottom center",
        "preview_color": "#ffffff",
        "name": "Classic",
        "ass_style": (
            "Style: Classic,Arial,56,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H64000000,0,0,0,0,100,100,0,0,1,3,1,2,40,40,30,1"
        ),
    },
    "tiktok_bold": {
        "label": "TikTok Bold",
        "description": "Huge bold white, thick black stroke, center screen",
        "preview_color": "#ffffff",
        "name": "TikTokBold",
        "ass_style": (
            "Style: TikTokBold,Arial Black,80,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,6,0,5,60,60,200,1"
        ),
    },
    "karaoke": {
        "label": "Karaoke",
        "description": "Word-by-word color highlight as spoken",
        "preview_color": "#ffff00",
        "name": "Karaoke",
        "ass_style": (
            "Style: Karaoke,Arial,60,&H00FFFFFF,&H0000FFFF,&H00000000,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,40,40,60,1"
        ),
    },
    "clean_box": {
        "label": "Clean Box",
        "description": "White text on semi-transparent dark background",
        "preview_color": "#ffffff",
        "name": "CleanBox",
        "ass_style": (
            "Style: CleanBox,Helvetica,54,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H96000000,-1,0,0,0,100,100,0,0,3,0,0,2,40,40,50,1"
        ),
    },
    "neon": {
        "label": "Neon",
        "description": "Cyan text with magenta glow outline",
        "preview_color": "#00ffff",
        "name": "Neon",
        "ass_style": (
            "Style: Neon,Arial,58,&H0000FFFF,&H000000FF,&H00FF00FF,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,4,0,2,40,40,50,1"
        ),
    },
    "minimal": {
        "label": "Minimal",
        "description": "Small clean text, subtle 1px outline",
        "preview_color": "#dddddd",
        "name": "Minimal",
        "ass_style": (
            "Style: Minimal,Arial,38,&H00DDDDDD,&H000000FF,&H00000000,"
            "&H00000000,0,0,0,0,100,100,1,0,1,1,0,2,40,40,30,1"
        ),
    },
    "cinematic": {
        "label": "Cinematic",
        "description": "Italic serif, fade in/out, top placement",
        "preview_color": "#ffffff",
        "name": "Cinematic",
        "ass_style": (
            "Style: Cinematic,Georgia,50,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H00000000,0,-1,0,0,100,100,3,0,1,2,2,8,80,80,120,1"
        ),
    },
    "duo_tone": {
        "label": "Duo Tone",
        "description": "White base text + oversized accent color on last words",
        "preview_color": "#ff2222",
        "name": "DuoTone",
        "compound": True,
        "words_per_group": 5,
        "split_after": 3,
        "two_line": True,
        # accent.color uses inline &H{BB}{GG}{RR} format (no alpha byte)
        "accent": {
            "color": "&H2222FF",  # vivid red: RGB(255,34,34)
            "scale_x": 130,
            "scale_y": 130,
        },
        "ass_style": (
            "Style: DuoTone,Arial Black,64,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,5,0,5,60,60,200,1"
        ),
    },
    "mixed_weight": {
        "label": "Mixed Weight",
        "description": "Solid bold fill on first words, hollow outline on last words",
        "preview_color": "#ffffff",
        "name": "MixedWeight",
        "compound": True,
        "words_per_group": 4,
        "split_after": 2,
        "two_line": True,
        # hollow=True: makes primary fill transparent, shows outline only
        "accent": {
            "hollow": True,
            "outline_color": "&HFFFFFF",  # white outline: RGB(255,255,255)
            "border_width": 8,
        },
        "ass_style": (
            "Style: MixedWeight,Arial Black,72,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H00000000,-1,0,0,0,100,100,0,0,1,7,0,5,60,60,200,1"
        ),
    },
}
