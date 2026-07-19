from thefuzz import fuzz
import _general
import re

ALBUM_KEYWORDS_TO_REMOVE = [
    "extended",
    "limited",
    "deluxe",
    "special",
    "remastered",
    "anniversary",
    "collector's",
    "ultimate",
    "bonus",
]
SONG_KEYWORDS_TO_REMOVE = [
    "radio",
    "limited",
    "remastered",
    "bonus",
    "feat",
    "featuring",
    "live",
    "edit",
    "version",
    "acoustic",
    "studio",
    "cover",
    "instrumental",
    "extended",
    "mix",
    "demo",
    "original",
    "reissue",
    "track",
    "official",
    "lyric",
]

# Version markers that mean "this is NOT the requested recording" — karaoke,
# instrumental, covers, etc. A candidate is rejected when its markers differ from
# the request's (see _version_mismatch), so a track that IS legitimately an
# instrumental still matches its own version but not the plain vocal, and vice versa.
UNWANTED_VERSION_MARKERS = [
    "instrumental",
    "karaoke",
    "backing track",
    "made famous by",
    "originally performed by",
    "in the style of",
    "tribute",
    "a cappella",
    "acapella",
    "8d audio",
    "sped up",
    "slowed",
    "nightcore",
    "cover",
]


def _contains_marker(text, marker):
    return re.search(r"\b" + re.escape(marker) + r"\b", text) is not None


# "Clean" is a censored radio version — treated like the other markers, but only in an
# explicit qualifier form: "(Clean)", "[Clean]", "Clean Version/Edit/Mix". Never a bare
# word, so songs legitimately named with "clean" ("Come Clean", "Mr. Clean", Taylor
# Swift's "Clean") are not mistaken for the censored cut.
_CLEAN_VERSION_RE = re.compile(r"[\(\[]\s*clean\b|\bclean\s+(?:version|edit|radio\s*edit|mix)\b", re.IGNORECASE)


def _has_clean_marker(text):
    return _CLEAN_VERSION_RE.search(text or "") is not None


def _version_mismatch(requested_title, candidate_title):
    """True if request and candidate disagree on any strong version marker.

    Symmetric: grabbing an instrumental/karaoke/cover/clean cut for a normal request
    AND grabbing the plain vocal for a request that explicitly wants that version are
    both rejected. Markers the request itself asks for are allowed through.
    """
    req = (requested_title or "").lower()
    cand = (candidate_title or "").lower()
    for marker in UNWANTED_VERSION_MARKERS:
        if _contains_marker(cand, marker) != _contains_marker(req, marker):
            return True
    if _has_clean_marker(cand) != _has_clean_marker(req):
        return True
    return False


def _normalize_min_ratio(minimum_match_ratio):
    """Accept a 0-100 percentage or a 0-1 fraction; always return the 0-100 scale.

    Guards against a settings value like 0.85 silently disabling the threshold,
    since match ratings are fuzz.ratio values on a 0-100 scale.
    """
    try:
        ratio = float(minimum_match_ratio)
    except (TypeError, ValueError):
        return 0
    if 0 < ratio <= 1:
        return ratio * 100
    return ratio


def _artist_in_result(cleaned_artist, item):
    """True if the artist appears in the candidate's title OR its uploader/channel.

    YouTube 'topic' and VEVO channels often title a track as just the song name,
    so gating on the title alone drops legitimate official uploads.
    """
    if not cleaned_artist:
        return True
    parts = [item.get("title", "")]
    for field in ("uploader", "channel", "uploader_id"):
        val = item.get(field)
        if isinstance(val, dict):
            val = val.get("name", "")
        if val:
            parts.append(str(val))
    haystack = _general.string_cleaner(" ".join(parts)).lower()
    return cleaned_artist in haystack


def _remove_keywords(text, keywords):
    ret = text
    for keyword in keywords:
        if keyword in ret:
            ret = re.sub(r"(\s*\(\s*)?(" + re.escape(keyword) + r")(?:\s*\))?", "", ret)
    return ret


def _normalized_text(text):
    return _general.string_cleaner(text).lower()


def _best_match_or_none(best_match_rating, minimum_match_ratio, best_match_item):
    if best_match_rating > _normalize_min_ratio(minimum_match_ratio):
        return best_match_item
    return None


def _parse_duration_string(duration_str):
    """Parse 'M:SS' or 'H:MM:SS' duration string to total seconds. Returns 0 on any failure."""
    if not duration_str:
        return 0
    try:
        parts = [int(p) for p in str(duration_str).strip().split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except (ValueError, AttributeError):
        pass
    return 0


def _duration_ok(expected_ms, candidate_seconds, tolerance_seconds):
    """True if candidate is within tolerance of expected, or if either side is unknown (0)."""
    if not expected_ms or not candidate_seconds:
        return True
    return abs(candidate_seconds - expected_ms / 1000.0) <= tolerance_seconds


def _append_trace(trace, source, item, candidate_seconds, score, rejected_by):
    """Record one candidate decision without changing matcher return contracts."""
    if trace is None:
        return
    video_id = item.get("videoId")
    candidate_url = item.get("webpage_url") or item.get("link")
    if not candidate_url and video_id:
        candidate_url = f"https://www.youtube.com/watch?v={video_id}"
    trace.append({
        "source": source,
        "candidate_title": item.get("title", ""),
        "candidate_url": candidate_url,
        "candidate_duration_s": candidate_seconds or 0,
        "score": score,
        "rejected_by": rejected_by,
    })


def remove_album_keywords(text):
    return _remove_keywords(text, ALBUM_KEYWORDS_TO_REMOVE)


def remove_song_keywords(text):
    return _remove_keywords(text, SONG_KEYWORDS_TO_REMOVE)


def album_matcher(minimum_match_ratio, artist, album_name, cleaned_artist, cleaned_album, search_results,
                  item_wanted_type="Album", trace=None):
    if not search_results:
        return None
    best_match_rating = 0
    best_match_item = None
    for item in search_results:
        if item["type"] != item_wanted_type:
            _append_trace(trace, "ytmusic", item, 0, None, "not_song_type")
            continue
        raw_album_match_ratio = fuzz.ratio(album_name, item["title"])
        artists_string = "".join([item["artists"][x]["name"] for x in range(1, len(item["artists"]))])
        raw_artist_match_ratio = fuzz.ratio(artist, artists_string)
        cleaned_yt_album_name = _normalized_text(item["title"])
        cleaned_album_match_ratio = fuzz.ratio(cleaned_album, cleaned_yt_album_name)
        cleaned_artists_string = _normalized_text(artists_string)
        cleaned_artist_match_ratio = fuzz.ratio(cleaned_artist, cleaned_artists_string)
        cleaned_yt_album_title_minus_keywords = remove_album_keywords(cleaned_yt_album_name)
        album_ratio_minus_keywords = fuzz.ratio(cleaned_album, cleaned_yt_album_title_minus_keywords)
        cleaned_yt_artist_minus_keywords = remove_album_keywords(cleaned_artists_string)
        artist_ratio_minus_keywords = fuzz.ratio(cleaned_artist, cleaned_yt_artist_minus_keywords)
        score = (raw_album_match_ratio + raw_artist_match_ratio + cleaned_album_match_ratio + cleaned_artist_match_ratio + album_ratio_minus_keywords + artist_ratio_minus_keywords) / 6
        _append_trace(trace, "ytmusic", item, 0, score, "accepted" if score > _normalize_min_ratio(minimum_match_ratio) else "below_threshold")
        if score > best_match_rating:
            best_match_rating = score
            best_match_item = item
            if score == 100:
                break
    return _best_match_or_none(best_match_rating, minimum_match_ratio, best_match_item)


def song_matcher(minimum_match_ratio, artist, cleaned_artist, song_title, cleaned_song_title, search_results,
                 item_wanted_type="song", expected_duration_ms=0, duration_tolerance_seconds=15, trace=None):
    if not search_results:
        return None
    best_match_rating = 0
    best_match_item = None
    cleaned_song_title_minus_keywords = remove_song_keywords(cleaned_song_title)
    threshold = _normalize_min_ratio(minimum_match_ratio)

    for item in search_results:
        candidate_seconds = item.get("duration_seconds") or 0
        if item["resultType"] != item_wanted_type:
            _append_trace(trace, "ytmusic", item, candidate_seconds, None, "not_song_type")
            continue
        if _version_mismatch(song_title, item["title"]):
            _append_trace(trace, "ytmusic", item, candidate_seconds, None, "version_gate")
            continue
        if not _duration_ok(expected_duration_ms, candidate_seconds, duration_tolerance_seconds):
            _append_trace(trace, "ytmusic", item, candidate_seconds, None, "duration_gate")
            continue
        artists_string = "".join([x["name"] for x in item["artists"]])
        raw_artist_match_ratio = fuzz.ratio(artist, artists_string)
        if artist.lower() in artists_string.lower():
            raw_artist_match_ratio = 100
        cleaned_artists_string = _normalized_text(artists_string)
        cleaned_artist_match_ratio = fuzz.ratio(cleaned_artist, cleaned_artists_string)
        cleaned_yt_song_title = _normalized_text(item["title"])
        cleaned_song_title_ratio = fuzz.ratio(cleaned_song_title, cleaned_yt_song_title)
        if song_title.lower() in item["title"].lower():
            cleaned_song_title_ratio = 100
        cleaned_yt_title_minus_keywords = remove_song_keywords(cleaned_yt_song_title)
        cleaned_song_title_minus_keywords_ratio = fuzz.ratio(cleaned_song_title_minus_keywords, cleaned_yt_title_minus_keywords)
        score = (raw_artist_match_ratio + cleaned_artist_match_ratio + cleaned_song_title_ratio + cleaned_song_title_minus_keywords_ratio) / 4
        _append_trace(trace, "ytmusic", item, candidate_seconds, score, "accepted" if score > threshold else "below_threshold")
        if score > best_match_rating:
            best_match_rating = score
            best_match_item = item
            if score == 100:
                break
    return _best_match_or_none(best_match_rating, minimum_match_ratio, best_match_item)


def song_matcher_yt(minimum_match_ratio, artist, query_text, search_results,
                    expected_duration_ms=0, duration_tolerance_seconds=15, trace=None):
    if not search_results:
        return None
    best_match_rating = 0
    best_match_item = None
    cleaned_query_text = _general.string_cleaner(query_text)
    cleaned_query_text_minus_keywords = remove_song_keywords(cleaned_query_text)
    cleaned_artist = _general.string_cleaner(artist).lower() if artist else ""
    threshold = _normalize_min_ratio(minimum_match_ratio)

    for item in search_results:
        title = item.get("title", "")
        raw_duration = item.get("duration", 0)
        candidate_seconds = _parse_duration_string(raw_duration) if isinstance(raw_duration, str) else int(raw_duration or 0)
        if not _artist_in_result(cleaned_artist, item):
            _append_trace(trace, "yt", item, candidate_seconds, None, "artist_gate")
            continue
        if _version_mismatch(query_text, title):
            _append_trace(trace, "yt", item, candidate_seconds, None, "version_gate")
            continue
        if not _duration_ok(expected_duration_ms, candidate_seconds, duration_tolerance_seconds):
            _append_trace(trace, "yt", item, candidate_seconds, None, "duration_gate")
            continue
        title_similarity = fuzz.ratio(query_text, title)
        if query_text in title:
            title_similarity = 100
        cleaned_title = _general.string_cleaner(title)
        cleaned_title_similarity = fuzz.ratio(cleaned_query_text, cleaned_title)
        if cleaned_query_text in cleaned_title:
            cleaned_title_similarity = 100
        cleaned_title_minus_keywords = remove_song_keywords(cleaned_title)
        cleaned_title_minus_keywords_similarity = fuzz.ratio(cleaned_query_text_minus_keywords, cleaned_title_minus_keywords)
        if cleaned_query_text_minus_keywords in cleaned_title_minus_keywords:
            cleaned_title_minus_keywords_similarity = 100
        score = (title_similarity + cleaned_title_similarity + cleaned_title_minus_keywords_similarity) / 3
        _append_trace(trace, "yt", item, candidate_seconds, score, "accepted" if score > threshold else "below_threshold")
        if score > best_match_rating:
            best_match_rating = score
            best_match_item = item
            if score == 100:
                break
    return _best_match_or_none(best_match_rating, minimum_match_ratio, best_match_item)
