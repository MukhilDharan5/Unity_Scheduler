"""Search and sort rules shared by the library's native views."""

from .core import LANGUAGES

FILTERS = ("All scripts", "Enabled", "Disabled", "Running", "Needs attention")
SORTS = ("Name · A–Z", "Recently added", "Last run", "Favorites first")


def matching_scripts(scripts, query="", status=0, sort=0, groups=None, running=()):
    words = query.casefold().split()
    result = []
    for script in scripts:
        active = script.id in running or script.last_status == "running"
        if status == 1 and not script.enabled or status == 2 and script.enabled:
            continue
        if status == 3 and not active:
            continue
        if status == 4 and not (script.missing or script.last_status in ("failed", "interrupted")):
            continue
        group = groups.get(script.id) if groups else None
        haystack = " ".join((script.name, script.path, LANGUAGES[script.language][0],
                             script.schedule_label, group.title if group else "",
                             script.last_status or "", "enabled" if script.enabled else "disabled")).casefold()
        if all(word in haystack for word in words):
            result.append(script)
    name = lambda script: (script.name.casefold(), script.path)
    key = {1: lambda script: (-script.added, name(script)),
           2: lambda script: (-(script.last_run or 0), name(script)),
           3: lambda script: (not script.favorite, name(script))}.get(sort, name)
    return sorted(result, key=key)
