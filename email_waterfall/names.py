"""Shared person-name normalizer for the pattern tier and LinkedIn matching.

Rules (Josh, Oct 25 2026): lowercase, strip accents and anything not a-z,
strip titles (Dr / Mr / Ms ...), strip suffixes (Jr Sr II III MD PhD RN DNP
MBA FACHE CPA DO NP) from the last name, take the first real token of the
first name and the last token of the last name. "R. Todd" -> "todd",
"Smith MD" -> "smith".
"""

from __future__ import annotations

import re
import unicodedata

TITLES = frozenset({"dr", "mr", "mrs", "ms", "miss", "mx", "prof", "rev", "sir"})
SUFFIXES = frozenset(
    {
        "jr", "sr", "ii", "iii", "iv", "md", "phd", "rn", "dnp", "mba",
        "fache", "cpa", "do", "np",
    }
)

# Common first-name nicknames. Only used when the last name matches exactly.
_NICKNAMES: dict[str, set[str]] = {
    "robert": {"rob", "bob", "bobby", "robbie", "bert"},
    "william": {"will", "bill", "billy", "liam", "willie"},
    "richard": {"rich", "rick", "dick", "ricky", "richie"},
    "james": {"jim", "jimmy", "jamie"},
    "john": {"jack", "johnny", "jon"},
    "michael": {"mike", "mikey", "mick"},
    "thomas": {"tom", "tommy"},
    "joseph": {"joe", "joey"},
    "charles": {"charlie", "chuck", "chas"},
    "christopher": {"chris", "topher"},
    "daniel": {"dan", "danny"},
    "matthew": {"matt"},
    "anthony": {"tony"},
    "steven": {"steve"},
    "stephen": {"steve"},
    "edward": {"ed", "eddie", "ted", "ned"},
    "andrew": {"andy", "drew"},
    "joshua": {"josh"},
    "benjamin": {"ben", "benny"},
    "samuel": {"sam", "sammy"},
    "alexander": {"alex", "al", "xander"},
    "nicholas": {"nick", "nicky"},
    "jonathan": {"jon", "jonny"},
    "timothy": {"tim", "timmy"},
    "patrick": {"pat", "paddy"},
    "gregory": {"greg"},
    "kenneth": {"ken", "kenny"},
    "ronald": {"ron", "ronnie"},
    "donald": {"don", "donny"},
    "douglas": {"doug"},
    "gerald": {"gerry", "jerry"},
    "lawrence": {"larry"},
    "raymond": {"ray"},
    "frederick": {"fred", "freddie"},
    "elizabeth": {"liz", "beth", "betsy", "lizzie", "eliza", "betty"},
    "katherine": {"kate", "kathy", "katie", "kat"},
    "catherine": {"cathy", "kate", "katie", "cat"},
    "margaret": {"maggie", "meg", "peggy", "marge"},
    "jennifer": {"jen", "jenny"},
    "jessica": {"jess", "jessie"},
    "rebecca": {"becky", "becca"},
    "susan": {"sue", "susie"},
    "patricia": {"pat", "patty", "trish"},
    "deborah": {"deb", "debbie"},
    "kimberly": {"kim"},
    "victoria": {"vicky", "tori"},
    "christine": {"chris", "chrissy", "tina"},
    "cynthia": {"cindy"},
    "pamela": {"pam"},
    "theodore": {"ted", "teddy", "theo"},
    "zachary": {"zach", "zack"},
}
_NICK_TO_FULL: dict[str, set[str]] = {}
for _full, _nicks in _NICKNAMES.items():
    for _n in _nicks:
        _NICK_TO_FULL.setdefault(_n, set()).add(_full)


def _tokens(value: str) -> list[str]:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    out = []
    for raw in re.split(r"[\s,]+", text):
        tok = re.sub(r"[^a-z]", "", raw)
        if tok:
            out.append(tok)
    return out


def norm_first(first: str) -> str:
    """First real token (>= 2 letters, not a title). 'R. Todd' -> 'todd'."""
    toks = [t for t in _tokens(first) if t not in TITLES]
    for tok in toks:
        if len(tok) >= 2:
            return tok
    return toks[0] if toks else ""


def norm_last(last: str) -> str:
    """Last token after stripping titles and suffixes. 'Smith MD' -> 'smith'."""
    toks = [t for t in _tokens(last) if t not in TITLES and t not in SUFFIXES]
    return toks[-1] if toks else ""


def first_names_compatible(a: str, b: str) -> bool:
    """Same first name, a known nickname, or one a 3+ letter prefix of the other."""
    a, b = norm_first(a), norm_first(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 3 and long_.startswith(short):
        return True
    if short in _NICKNAMES.get(long_, set()) or long_ in _NICKNAMES.get(short, set()):
        return True
    if _NICK_TO_FULL.get(a, set()) & _NICK_TO_FULL.get(b, set()):
        return True
    return False


def names_match(first: str, last: str, cand_first: str, cand_last: str) -> bool:
    """Last name must match exactly; the first name may differ only by a nickname
    or a middle initial. Never true on last name alone."""
    l1, l2 = norm_last(last), norm_last(cand_last)
    if not l1 or l1 != l2:
        return False
    return first_names_compatible(first, cand_first)
