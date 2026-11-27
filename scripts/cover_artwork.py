"""Review artwork candidates and replace embedded images without encoding audio."""

from functools import lru_cache
from io import BytesIO
import shutil

from PIL import Image
from mutagen import File
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4, MP4Cover
from zxingcpp import BarcodeFormat, read_barcodes

from media_analysis import audio_packet_hash, probe


@lru_cache(maxsize=64)
def prominent_qr(data):
    """Find a QR used as the main artwork, even when placed above a cover."""
    with Image.open(BytesIO(data)) as image:
        image.thumbnail((1000, 1000))
        for code in read_barcodes(image.convert("L"), formats=BarcodeFormat.QRCode):
            pos = code.position
            points = (pos.top_left, pos.top_right, pos.bottom_left, pos.bottom_right)
            width = max(point.x for point in points) - min(point.x for point in points)
            height = max(point.y for point in points) - min(point.y for point in points)
            if width * height >= image.width * image.height * 0.12:
                return True
    return False


def embedded_covers(audio):
    tags = getattr(audio, "tags", None)
    if isinstance(audio, FLAC):
        return [picture.data for picture in audio.pictures]
    if isinstance(audio, MP4):
        return [bytes(picture) for picture in (tags or {}).get("covr", [])]
    if isinstance(tags, ID3):
        return [picture.data for picture in tags.getall("APIC")]
    return []


def inspect_music_artwork(path, expected):
    try:
        pictures = embedded_covers(File(path))
        if any(prominent_qr(data) for data in pictures):
            return "qr"
    except (OSError, ValueError, Image.DecompressionBombError):
        return "unreadable"
    if expected and pictures != [expected] and path.suffix.lower() != ".aac":
        return "mismatch"
    return None


def music_with_cover(source, destination, artwork):
    """Keep audio packets and playback metadata while normalizing the cover tag."""
    audio = File(source)
    data = artwork.read_bytes()
    if embedded_covers(audio) == [data] or source.suffix.lower() == ".aac":
        return source
    mime = "image/png" if artwork.suffix.lower() == ".png" else "image/jpeg"
    shutil.copyfile(source, destination)
    try:
        edited = File(destination)
        if isinstance(edited, FLAC):
            with Image.open(BytesIO(data)) as image:
                picture = Picture()
                picture.type, picture.mime, picture.data = 3, mime, data
                picture.width, picture.height = image.size
                picture.depth = 8 * len(image.getbands())
            edited.clear_pictures()
            edited.add_picture(picture)
        elif isinstance(edited, MP4):
            if edited.tags is None:
                edited.add_tags()
            encoding = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            edited.tags["covr"] = [MP4Cover(data, imageformat=encoding)]
        elif destination.suffix.lower() in {".mp3", ".aif", ".aiff", ".wav"} and edited is not None:
            if edited.tags is None:
                edited.add_tags()
            edited.tags.delall("APIC")
            edited.tags.add(APIC(mime=mime, type=3, desc="", data=data))
        else:
            raise ValueError(f"此音频格式无法写入封面：{source.name}")
        edited.save()
        previous, _ = probe(source, "audio")
        current, _ = probe(destination, "audio")
        if (embedded_covers(File(destination)) != [data] or abs(previous - current) > 0.001
                or audio_packet_hash(source) != audio_packet_hash(destination)):
            raise ValueError(f"更换封面后音频复测未通过：{source.name}")
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise
