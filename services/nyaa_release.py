import re


DEFAULT_FEED_URL = "https://nyaa.land/?page=rss"
AUDIO_MISMATCH = "ترک‌های صوتی انیمه با تنظیم عنوان سازگار نیستند"

_GROUP_PREFIX = re.compile(r"^\s*\[[^\]]+\]\s*")
_NUMBER = r"(?P<{}>\d{{1,3}})(?:v\d+)?(?!\w)"
_EPISODE = re.compile(
    rf"(?:[ ._-]+S(?P<season>\d{{1,2}})[ ._-]*EP?{_NUMBER.format('episode')}"
    rf"|[ ._-]+(?P<alt_season>\d{{1,2}})x{_NUMBER.format('alt_episode')}"
    rf"|[ ._-]+(?:EP?(?:isode)?[ ._-]*|-[ ._]*|(?=\d))"
    rf"{_NUMBER.format('only_episode')}"
    rf"|\s*[\[(]\s*{_NUMBER.format('bracket_episode')}\s*[\])])",
    re.I,
)
_MULTI_EPISODE = re.compile(r"^(?:[ ._]*[-–+&,~][ ._-]*E?\d{1,3}(?!\w)|[ ._]*E\d)", re.I)
_MULTISUB = re.compile(r"\b(?:multi[ ._-]*subs?|multiple subtitles|multilingual subtitles)\b", re.I)
_EN_SUB = re.compile(r"\b(?:eng(?:lish)?[ ._-]*(?:subs?|subtitles)|(?:subs?|subtitles)[ ._-]*eng(?:lish)?|engsub)\b", re.I)
_DUAL_AUDIO = re.compile(r"\b(?:dual[ ._-]*audio|multi[ ._-]*audio|(?:jpn|jap|ja|japanese)\s*[/+&-]\s*(?:eng|en|english)|(?:eng|en|english)\s*[/+&-]\s*(?:jpn|jap|ja|japanese))\b", re.I)
_DUB_ONLY = re.compile(r"\b(?:dub(?:bed)?|(?:eng(?:lish)?|en)[ ._-]*audio)\b", re.I)
_JAPANESE_AUDIO = re.compile(r"\b(?:jpn|japanese|ja)[ ._-]*audio\b", re.I)
_EDITION = re.compile(r"\s*\[(?:un)?censored\]\s*$", re.I)
_PARENTHETICAL_TITLE = re.compile(r"^(.+?)\s+\(([^()]+)\)$")


def _title_key(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _release_title_keys(prefix: str) -> set[str]:
    prefix = _EDITION.sub("", prefix).strip()
    variants = [prefix, _GROUP_PREFIX.sub("", prefix)]
    keys = {_title_key(value) for value in variants}
    for variant in variants:
        titles = _PARENTHETICAL_TITLE.fullmatch(variant)
        if titles:
            keys.update(_title_key(value) for value in titles.groups())
    return keys


def episode_and_metadata(title: str, show: dict) -> tuple[int, str] | None:
    name = re.sub(r"\.(?:mkv|mp4|avi|webm|m4v|ts|mov)$", "", title, flags=re.I)
    season = show["season"]
    primary = {_title_key(show["title"])}
    aliases = {_title_key(alias) for alias in show.get("aliases", [])} - primary
    names = (primary | aliases) - {""}
    season_suffix = re.compile(
        rf"[ ._-]+(?:s0?{season}|season[ ._-]*0?{season}|{season}(?:st|nd|rd|th)[ ._-]+season)$",
        re.I,
    )
    for match in _EPISODE.finditer(name):
        prefix = name[:match.start()]
        title_keys = _release_title_keys(prefix)
        groups = match.groupdict()
        explicit_season = groups["season"] or groups["alt_season"]
        if explicit_season:
            if int(explicit_season) != season or not title_keys & names:
                continue
        elif not (
            (season == 1 and title_keys & names)
            or title_keys & aliases
            or (season_suffix.search(prefix) and _release_title_keys(season_suffix.sub("", prefix)) & names)
        ):
            continue
        metadata = name[match.end():]
        if _MULTI_EPISODE.match(metadata) or re.match(r"^\.\d+\b", metadata):
            return None
        number = next(groups[key] for key in (
            "episode", "alt_episode", "only_episode", "bracket_episode",
        ) if groups[key] is not None)
        return int(number), metadata
    return None


def release_priority(release: dict, metadata: str, dual_audio: bool) -> tuple[int, bool, int] | None:
    seeders = release["seeders"]
    if seeders == 0:
        return None
    category_id = release["category_id"]
    if category_id and category_id not in {"1_1", "1_2", "1_3", "1_4"}:
        return None
    category = release["category"].casefold()
    if category and category != "anime" and not category.startswith("anime - "):
        return None
    dual = bool(_DUAL_AUDIO.search(metadata))
    if not dual and _DUB_ONLY.search(metadata) and not _JAPANESE_AUDIO.search(metadata):
        return None
    if dual and not dual_audio:
        return None
    if _MULTISUB.search(metadata):
        subtitles = 2
    elif _EN_SUB.search(metadata) or category_id == "1_2" or category == "anime - english-translated":
        subtitles = 1
    else:
        subtitles = 0
    return subtitles, dual, seeders or 0


def audio_matches(tracks: list[dict], dual_audio: bool) -> bool:
    languages = [
        str(props.get("language_ietf") or props.get("language") or "")
        .strip().casefold().split("-")[0]
        for track in tracks
        if track.get("type") == "audio"
        for props in [track.get("properties") or {}]
    ]
    japanese = {"ja", "jpn", "japanese"}
    if dual_audio:
        return any(lang in japanese for lang in languages) and any(
            lang in {"en", "eng", "english"} for lang in languages
        )
    return bool(languages) and all(lang in japanese for lang in languages)
