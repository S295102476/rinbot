"""Pure routing helpers shared by translation code and tests."""


def select_translation_provider(img_bytes: bytes | None) -> str:
    return "primary"
