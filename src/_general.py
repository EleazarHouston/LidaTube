import re
import unidecode
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TYER, TRCK, TCON
from mutagen.flac import FLAC


def convert_to_lidarr_format(input_string):
    bad_characters = r'\/<>?*:|"'
    good_characters = "++  !--  "
    translation_table = str.maketrans(bad_characters, good_characters)
    result = input_string.translate(translation_table)
    return result.strip()


def _clean_single_string(s):
    s = re.sub(r'[\/:*?"<>|]', " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return unidecode.unidecode(s)


def string_cleaner(input_string):
    if isinstance(input_string, str):
        return _clean_single_string(input_string)
    if isinstance(input_string, list):
        return [_clean_single_string(s) for s in input_string]
    return None


def add_metadata(logger, song, req_album, full_file_path):
    try:
        file_extension = re.search(r"\.[^.]*$", full_file_path).group().lower()

        if file_extension == ".flac":
            audio = FLAC(full_file_path)
            audio["title"] = song["track_title"]
            audio["tracknumber"] = str(song["track_number"])
            audio["artist"] = song["artist"]
            audio["albumartist"] = req_album["artist"]
            audio["album"] = req_album["album_name"]
            audio["date"] = str(req_album["album_year"])
            audio["genre"] = req_album["album_genres"]
            audio.save()

        elif file_extension == ".mp3":
            metadata = ID3(full_file_path)
            metadata.add(TIT2(encoding=3, text=song["track_title"]))
            metadata.add(TRCK(encoding=3, text=str(song["track_number"])))
            metadata.add(TPE1(encoding=3, text=song["artist"]))
            metadata.add(TPE2(encoding=3, text=req_album["artist"]))
            metadata.add(TALB(encoding=3, text=req_album["album_name"]))
            metadata.add(TYER(encoding=3, text=str(req_album["album_year"])))
            metadata.add(TCON(encoding=3, text=str(req_album["album_genres"])))
            metadata.save()

        logger.warning(f"Metadata added for {full_file_path}")

    except Exception as e:
        logger.error(f"Error adding metadata for {full_file_path}: {e}")


def is_resource_exhaustion_error(error):
    if error is None:
        return False
    errnos = {23, 24}
    queue = [error]
    visited = set()
    while queue:
        current = queue.pop()
        current_id = id(current)
        if current_id in visited:
            continue
        visited.add(current_id)

        if isinstance(current, OSError) and current.errno in errnos:
            return True
        if "no file descriptors available" in str(current).lower():
            return True

        for attr in ("__cause__", "__context__"):
            related = getattr(current, attr, None)
            if related is not None:
                queue.append(related)
        for arg in getattr(current, "args", ()):
            if isinstance(arg, BaseException):
                queue.append(arg)

    return False
