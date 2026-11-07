from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_IMAGE_DIMENSION = 1600
WEBP_QUALITY = 82


def optimize_product_image(content: bytes) -> bytes:
    """Validate and convert an uploaded product image to a compact WebP."""
    try:
        with Image.open(BytesIO(content)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Поддерживаются только JPG, PNG и WEBP.")
            source.load()
            image = ImageOps.exif_transpose(source)
            image.thumbnail(
                (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            if "A" in image.getbands():
                image = image.convert("RGBA")
            else:
                image = image.convert("RGB")
            output = BytesIO()
            image.save(
                output,
                format="WEBP",
                quality=WEBP_QUALITY,
                method=6,
                exact=True,
            )
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Не удалось обработать изображение. Загрузи обычное фото JPG, PNG или WEBP.") from exc
