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
    "8d audio",
    "sped up",
    "slowed",
    "nightcore",
    "cover",
]


_A_CAPPELLA_RE = re.compile(r"\ba\s*c+ap+el+a\b")


def _canonical_markers(text):
    """Collapse spelling variants of a marker so "Acapella" and "a cappella" agree."""
    return _A_CAPPELLA_RE.sub("a cappella", text)


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
    req = _canonical_markers((requested_title or "").lower())
    cand = _canonical_markers((candidate_title or "").lower())
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
            parts.append(_youtube_channel_credit(str(val)))
    return _artist_credited(cleaned_artist, parts)


def _youtube_channel_credit(name):
    # VEVO commonly appends its branding without a separator (e.g. NasVEVO).
    return re.sub(r"vevo$", "", name, flags=re.IGNORECASE).strip()


def _artist_credited(cleaned_artist, artist_names):
    """True if the cleaned artist appears as whole words in any credited artist name.

    Handles collaborations ("Neil Young" in ["Neil Young", "Crazy Horse"]) and billing
    variants ("Nelson Riddle" in "Nelson Riddle & His Orchestra") without crediting
    "Nas" for "Jonas Brothers".
    """
    if not cleaned_artist:
        return False
    # Lookarounds rather than \b so names edged with punctuation ("M.I.A.", "fun.", "!!!") still bound correctly.
    pattern = r"(?<!\w)" + re.escape(cleaned_artist) + r"(?!\w)"
    return any(re.search(pattern, _normalized_text(name)) for name in artist_names)


def _remove_keywords(text, keywords):
    ret = text
    for keyword in keywords:
        if keyword in ret:
            ret = re.sub(r"(\s*\(\s*)?(" + re.escape(keyword) + r")(?:\s*\))?", "", ret)
    return ret


def _normalized_text(text):
    return _general.string_cleaner(text).lower()


def _best_match_or_none(best_match_rating, minimum_match_ratio, best_match_item):
    if best_match_rating >= _normalize_min_ratio(minimum_match_ratio):
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


# Words that only describe a release/format of the same recording. A bracketed group or
# " - suffix" made up solely of these (and numbers) is dropped whole, so "(album version)",
# "(5.1 mix)" or "- 2009 Remaster" don't leave fragments like "downtown (album " behind.
_QUALIFIER_WORDS = set(SONG_KEYWORDS_TO_REMOVE) | {
    "album", "lp", "single", "mono", "stereo", "remaster", "dirty", "explicit", "clean",
    "audio", "video", "lyrics", "visualizer", "hd", "hq", "digital", "take", "alternate",
    "alternative", "short", "long", "full", "mixed", "master",
}
_BRACKET_GROUP_RE = re.compile(r"\s*[\(\[]([^\(\)\[\]]*)[\)\]]")
_DASH_SUFFIX_RE = re.compile(r"\s+-\s+([^-]*)$")
_FEAT_GROUP_RE = re.compile(r"^\s*(?:feat|featuring|ft)\b", re.IGNORECASE)
_SONG_KEYWORD_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in SONG_KEYWORDS_TO_REMOVE) + r")\b", re.IGNORECASE)


def _is_qualifier_group(content):
    if _FEAT_GROUP_RE.match(content):
        return True
    tokens = re.findall(r"[a-z]+|\d+", content.lower())
    return bool(tokens) and all(token.isdigit() or token in _QUALIFIER_WORDS for token in tokens)


def remove_song_keywords(text):
    ret = _BRACKET_GROUP_RE.sub(lambda m: "" if _is_qualifier_group(m.group(1)) else m.group(0), text)
    ret = _DASH_SUFFIX_RE.sub(lambda m: "" if _is_qualifier_group(m.group(1)) else m.group(0), ret)
    ret = _SONG_KEYWORD_RE.sub("", ret)
    ret = re.sub(r"[\(\[]\s*[\)\]]", "", ret)
    ret = re.sub(r"([\(\[])\s+", r"\1", ret)
    ret = re.sub(r"\s+([\)\]])", r"\1", ret)
    return re.sub(r"\s+", " ", ret).strip()


# A bracketed group or " - suffix" that names a kind of version ("Spanish Version",
# "Solo Version", "Remix #1", "Live In Germany") carries descriptor words that identify
# a different recording. Qualifier stripping discards them for scoring, so they are
# compared separately: every descriptor must appear somewhere in the other title.
_VERSION_TYPE_WORDS = {
    "version", "mix", "remix", "edit", "dub", "rework", "recording", "session", "sessions",
    "take", "live", "acoustic", "demo", "unplugged",
}
_DISTINCT_RECORDING_WORDS = {"remix", "dub"}
_DESCRIPTOR_IGNORED_WORDS = _QUALIFIER_WORDS | _VERSION_TYPE_WORDS | {"deluxe", "edition", "expanded", "anniversary", "special"}
_DESCRIPTOR_EXEMPT_GROUP_RE = re.compile(r"^\s*(?:feat|featuring|ft|from)\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z]+|\d+")


def _version_descriptors(title):
    text = _normalized_text(title or "")
    groups = _BRACKET_GROUP_RE.findall(text)
    suffix = _DASH_SUFFIX_RE.search(text)
    if suffix:
        groups.append(suffix.group(1))
    descriptors = set()
    for group in groups:
        if _DESCRIPTOR_EXEMPT_GROUP_RE.match(group):
            continue
        words = _WORD_RE.findall(group)
        if not _VERSION_TYPE_WORDS.intersection(words):
            continue
        descriptors.update(
            word for word in words
            if not word.isdigit() and (word in _DISTINCT_RECORDING_WORDS or word not in _DESCRIPTOR_IGNORED_WORDS)
        )
    return descriptors, set(_WORD_RE.findall(text))


def _descriptor_present(word, title_words):
    if word in _DISTINCT_RECORDING_WORDS:
        return bool(title_words & {word, "mix", "remix"})
    return word in title_words


def _version_descriptor_mismatch(requested_title, candidate_title):
    """True if either title names a version (e.g. "Spanish Version", "Remix") the other lacks."""
    requested_descriptors, requested_words = _version_descriptors(requested_title)
    candidate_descriptors, candidate_words = _version_descriptors(candidate_title)
    return (
        any(not _descriptor_present(word, candidate_words) for word in requested_descriptors)
        or any(not _descriptor_present(word, requested_words) for word in candidate_descriptors)
    )


def _recording_mismatch(requested_title, candidate_title):
    """Shared recording validation for both search providers."""
    return (
        _version_mismatch(requested_title, candidate_title)
        or _version_descriptor_mismatch(requested_title, candidate_title)
    )


def _without_artist_prefix(title, artist):
    """Keep artist billing out of recording descriptors in YouTube search titles."""
    if not artist:
        return title
    return re.sub(r"^" + re.escape(artist) + r"\s+-\s+", "", title, count=1, flags=re.IGNORECASE)


# Live performances are never acceptable stand-ins for a studio recording: a candidate is
# live when a bracketed/dash group says so ("(Live)", "(BBC Session)"), the title uses a
# live phrase ("Live at Wembley", "In Concert", "Unplugged"), or its album is a live album.
_LIVE_PHRASE_RE = re.compile(r"\blive\s+(?:at|in|from|on)\b|\bin concert\b|\bunplugged\b")
_LIVE_GROUP_RE = re.compile(r"\blive\b|\bconcert\b|\b(?:bbc|peel) sessions?\b")
# Other non-original recordings rank below the original but remain acceptable when nothing
# else matches; only words inside bracketed/dash groups count, never the song name itself.
_NON_ORIGINAL_WORDS = {
    "acoustic", "demo", "remix", "rerecorded", "rerecording", "orchestral", "orchestra", "symphonic",
    "philharmonic", "stripped", "rehearsal", "alternate", "alternative", "early", "piano", "reimagined", "redux",
}
_NON_ORIGINAL_PENALTY = 10
_MAX_NON_ORIGINAL_PENALTY = 20
_STUDIO_FOR_LIVE_REQUEST_PENALTY = 15


def _title_groups(text):
    groups = _BRACKET_GROUP_RE.findall(text)
    suffix = _DASH_SUFFIX_RE.search(text)
    if suffix:
        groups.append(suffix.group(1))
    return groups


def _is_live_title(title):
    text = _normalized_text(title or "")
    if _LIVE_PHRASE_RE.search(text):
        return True
    return any(_LIVE_GROUP_RE.search(group) for group in _title_groups(text))


def _is_live_album(album_name):
    text = _normalized_text(album_name or "")
    if not text:
        return False
    return bool(re.search(r"\blive$", text)) or _is_live_title(text)


def _request_is_live(song_title, album_name=None, album_secondary_types=None):
    return (
        _is_live_title(song_title)
        or _is_live_album(album_name)
        or any(str(kind).lower() == "live" for kind in (album_secondary_types or []))
    )


def _candidate_album_name(item):
    album = item.get("album")
    if isinstance(album, dict):
        return album.get("name")
    return album if isinstance(album, str) else None


def _non_original_penalty(requested_title, candidate_title):
    """Ranking penalty for recording-changing words the candidate adds (acoustic, demo, orchestra...)."""
    requested_words = set(_WORD_RE.findall(_normalized_text(requested_title or "").replace("re-record", "rerecord")))
    added = set()
    for group in _title_groups(_normalized_text(candidate_title or "").replace("re-record", "rerecord")):
        added.update(word for word in _WORD_RE.findall(group) if word in _NON_ORIGINAL_WORDS and word not in requested_words)
    return min(len(added) * _NON_ORIGINAL_PENALTY, _MAX_NON_ORIGINAL_PENALTY)


def _recording_rank_penalty(requested_title, candidate_title, requested_live, candidate_live):
    penalty = _non_original_penalty(requested_title, candidate_title)
    if requested_live and not candidate_live:
        penalty += _STUDIO_FOR_LIVE_REQUEST_PENALTY
    return penalty


def _base_title(text):
    text = re.sub(r"\s*&\s*", " and ", (text or "").lower())
    text = re.sub(r"[^\w\s]", " ", remove_song_keywords(text))
    return re.sub(r"\s+", " ", text).strip()


def _same_base_title(left, right):
    """True if both titles are the same once release qualifiers, "&" and punctuation are ignored."""
    left_base = _base_title(left)
    return bool(left_base) and left_base == _base_title(right)


def album_matcher(minimum_match_ratio, artist, album_name, cleaned_artist, cleaned_album, search_results,
                  item_wanted_type="Album", trace=None, album_secondary_types=None):
    if not search_results:
        return None
    best_match_rating = 0
    best_match_item = None
    requested_live = _request_is_live("", album_name, album_secondary_types)
    for item in search_results:
        if item["type"] != item_wanted_type:
            _append_trace(trace, "ytmusic", item, 0, None, "not_song_type")
            continue
        if _is_live_album(item.get("title")) and not requested_live:
            _append_trace(trace, "ytmusic", item, 0, None, "live_gate")
            continue
        raw_album_match_ratio = fuzz.ratio(album_name, item["title"])
        artist_names = [entry["name"] for entry in item.get("artists", [])]
        artists_string = " ".join(artist_names)
        artist_credited = _artist_credited(cleaned_artist, artist_names)
        raw_artist_match_ratio = 100 if artist_credited else fuzz.ratio(artist, artists_string)
        cleaned_yt_album_name = _normalized_text(item["title"])
        cleaned_album_match_ratio = fuzz.ratio(cleaned_album, cleaned_yt_album_name)
        cleaned_artists_string = _normalized_text(artists_string)
        cleaned_artist_match_ratio = 100 if artist_credited else fuzz.ratio(cleaned_artist, cleaned_artists_string)
        cleaned_yt_album_title_minus_keywords = remove_album_keywords(cleaned_yt_album_name)
        album_ratio_minus_keywords = fuzz.ratio(cleaned_album, cleaned_yt_album_title_minus_keywords)
        cleaned_yt_artist_minus_keywords = remove_album_keywords(cleaned_artists_string)
        artist_ratio_minus_keywords = 100 if artist_credited else fuzz.ratio(cleaned_artist, cleaned_yt_artist_minus_keywords)
        score = (raw_album_match_ratio + raw_artist_match_ratio + cleaned_album_match_ratio + cleaned_artist_match_ratio + album_ratio_minus_keywords + artist_ratio_minus_keywords) / 6
        _append_trace(trace, "ytmusic", item, 0, score, "accepted" if score >= _normalize_min_ratio(minimum_match_ratio) else "below_threshold")
        if score > best_match_rating:
            best_match_rating = score
            best_match_item = item
            if score == 100:
                break
    return _best_match_or_none(best_match_rating, minimum_match_ratio, best_match_item)


def song_matcher(minimum_match_ratio, artist, cleaned_artist, song_title, cleaned_song_title, search_results,
                 item_wanted_type="song", expected_duration_ms=0, duration_tolerance_seconds=15, trace=None,
                 album_name=None, album_secondary_types=None):
    if not search_results:
        return None
    best_match_rating = 0
    best_match_item = None
    best_match_key = None
    requested_live = _request_is_live(song_title, album_name, album_secondary_types)
    cleaned_song_title_minus_keywords = remove_song_keywords(cleaned_song_title)
    threshold = _normalize_min_ratio(minimum_match_ratio)

    for item in search_results:
        candidate_seconds = item.get("duration_seconds") or 0
        if item["resultType"] != item_wanted_type:
            _append_trace(trace, "ytmusic", item, candidate_seconds, None, "not_song_type")
            continue
        candidate_live = _is_live_title(item["title"]) or _is_live_album(_candidate_album_name(item))
        if candidate_live and not requested_live:
            _append_trace(trace, "ytmusic", item, candidate_seconds, None, "live_gate")
            continue
        if _recording_mismatch(song_title, item["title"]):
            _append_trace(trace, "ytmusic", item, candidate_seconds, None, "version_gate")
            continue
        if not _duration_ok(expected_duration_ms, candidate_seconds, duration_tolerance_seconds):
            _append_trace(trace, "ytmusic", item, candidate_seconds, None, "duration_gate")
            continue
        artist_names = [x["name"] for x in item["artists"]]
        artists_string = "".join(artist_names)
        artist_credited = _artist_credited(cleaned_artist, artist_names) or _artist_credited(_normalized_text(artist), artist_names)
        raw_artist_match_ratio = fuzz.ratio(artist, artists_string)
        cleaned_artists_string = _normalized_text(artists_string)
        cleaned_artist_match_ratio = fuzz.ratio(cleaned_artist, cleaned_artists_string)
        # Similarities before the full-credit overrides: equal scores are broken in favour of
        # the closest artist list and title, so "Scared of Love" by Nate Dogg beats the same
        # title credited to Butch Cassidy & Nate Dogg, and "Buon Natale" beats its later duet.
        artist_similarity = cleaned_artist_match_ratio
        if artist_credited:
            raw_artist_match_ratio = cleaned_artist_match_ratio = 100
        cleaned_yt_song_title = _normalized_text(item["title"])
        cleaned_song_title_ratio = fuzz.ratio(cleaned_song_title, cleaned_yt_song_title)
        title_similarity = cleaned_song_title_ratio
        if song_title.lower() in item["title"].lower() or _same_base_title(cleaned_song_title, cleaned_yt_song_title):
            cleaned_song_title_ratio = 100
        cleaned_yt_title_minus_keywords = remove_song_keywords(cleaned_yt_song_title)
        cleaned_song_title_minus_keywords_ratio = fuzz.ratio(cleaned_song_title_minus_keywords, cleaned_yt_title_minus_keywords)
        score = (raw_artist_match_ratio + cleaned_artist_match_ratio + cleaned_song_title_ratio + cleaned_song_title_minus_keywords_ratio) / 4
        # Acceptance uses the raw score; ranking subtracts penalties for non-original recordings
        # (acoustic, orchestra...) and for studio takes when a live recording was requested.
        penalty = _recording_rank_penalty(song_title, item["title"], requested_live, candidate_live)
        match_key = (score - penalty, -penalty, artist_similarity + title_similarity)
        _append_trace(trace, "ytmusic", item, candidate_seconds, score, "accepted" if score >= threshold else "below_threshold")
        if best_match_key is None or match_key > best_match_key:
            best_match_key = match_key
            best_match_rating = score
            best_match_item = item
            if match_key == (100, 0, 200):
                break
    return _best_match_or_none(best_match_rating, minimum_match_ratio, best_match_item)


def _channel_name(item):
    for field in ("channel", "uploader", "uploader_id"):
        val = item.get(field)
        if isinstance(val, dict):
            val = val.get("name", "")
        if val:
            return str(val)
    return ""


def _yt_title_score(query_text, cleaned_query, cleaned_query_mk, candidate_text):
    """Three-way fuzzy score of a candidate text against the query, with substring bonuses."""
    title_similarity = fuzz.ratio(query_text, candidate_text)
    if query_text in candidate_text:
        title_similarity = 100
    cleaned = _general.string_cleaner(candidate_text)
    cleaned_similarity = fuzz.ratio(cleaned_query, cleaned)
    if cleaned_query in cleaned:
        cleaned_similarity = 100
    cleaned_mk = remove_song_keywords(cleaned)
    cleaned_mk_similarity = fuzz.ratio(cleaned_query_mk, cleaned_mk)
    if cleaned_query_mk in cleaned_mk:
        cleaned_mk_similarity = 100
    return (title_similarity + cleaned_similarity + cleaned_mk_similarity) / 3


def song_matcher_yt(minimum_match_ratio, artist, query_text, search_results,
                    expected_duration_ms=0, duration_tolerance_seconds=15, trace=None,
                    album_name=None, album_secondary_types=None):
    if not search_results:
        return None
    best_match_rating = 0
    best_match_item = None
    best_match_key = None
    cleaned_query_text = _general.string_cleaner(query_text)
    cleaned_query_text_minus_keywords = remove_song_keywords(cleaned_query_text)
    cleaned_artist = _general.string_cleaner(artist).lower() if artist else ""
    threshold = _normalize_min_ratio(minimum_match_ratio)
    gate_cleared = []
    requested_title = _without_artist_prefix(query_text, artist)
    requested_live = _request_is_live(requested_title, album_name, album_secondary_types)

    for item in search_results:
        title = item.get("title", "")
        raw_duration = item.get("duration", 0)
        candidate_seconds = _parse_duration_string(raw_duration) if isinstance(raw_duration, str) else int(raw_duration or 0)
        if not _artist_in_result(cleaned_artist, item):
            _append_trace(trace, "yt", item, candidate_seconds, None, "artist_gate")
            continue
        candidate_live = _is_live_title(_without_artist_prefix(title, artist))
        if candidate_live and not requested_live:
            _append_trace(trace, "yt", item, candidate_seconds, None, "live_gate")
            continue
        if _recording_mismatch(requested_title, _without_artist_prefix(title, artist)):
            _append_trace(trace, "yt", item, candidate_seconds, None, "version_gate")
            continue
        if not _duration_ok(expected_duration_ms, candidate_seconds, duration_tolerance_seconds):
            _append_trace(trace, "yt", item, candidate_seconds, None, "duration_gate")
            continue
        score = _yt_title_score(query_text, cleaned_query_text, cleaned_query_text_minus_keywords, title)
        _append_trace(trace, "yt", item, candidate_seconds, score, "accepted" if score >= threshold else "below_threshold")
        penalty = _recording_rank_penalty(requested_title, _without_artist_prefix(title, artist), requested_live, candidate_live)
        gate_cleared.append((item, title, candidate_seconds, penalty))
        match_key = (score - penalty, -penalty)
        if best_match_key is None or match_key > best_match_key:
            best_match_key = match_key
            best_match_rating = score
            best_match_item = item
            if match_key == (100, 0):
                break

    match = _best_match_or_none(best_match_rating, minimum_match_ratio, best_match_item)
    if match is not None:
        return match

    # Fallback: official/topic uploads credit the artist in the CHANNEL, not the title
    # (e.g. "♪ Diggy Diggy Hole" on channel "The Yogscast"), so the artist prefix in the
    # query has nothing to match and the score falls short. Re-score the gate-cleared
    # candidates with the channel name plugged in for the artist; the title must still
    # match, so a wrong song on the right channel won't pass.
    if not cleaned_artist:
        return None
    fb_rating = 0
    fb_item = None
    fb_key = None
    for item, title, candidate_seconds, penalty in gate_cleared:
        channel = _channel_name(item)
        if not channel or not _artist_credited(cleaned_artist, [_youtube_channel_credit(channel)]):
            continue
        augmented = f"{channel} - {title}"
        score = _yt_title_score(query_text, cleaned_query_text, cleaned_query_text_minus_keywords, augmented)
        _append_trace(trace, "yt-channel", item, candidate_seconds, score, "accepted" if score >= threshold else "below_threshold")
        match_key = (score - penalty, -penalty)
        if fb_key is None or match_key > fb_key:
            fb_key = match_key
            fb_rating = score
            fb_item = item
            if match_key == (100, 0):
                break
    return _best_match_or_none(fb_rating, minimum_match_ratio, fb_item)
